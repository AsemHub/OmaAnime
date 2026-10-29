"""Indexer add-ons: the plugin ships no indexer; the viewer drops Python files into
`<config>/indexers/`, and every release the plugin considers comes from one of them.

An add-on declares `API_VERSION` (an int this module supports), `NAME` (display name, optional:
the file's stem otherwise) and a required `episode_search(ctx, query) -> list[dict]`. `ctx` carries
the plugin's `http` client, the play's `log` and a read-only copy of the general `settings`, so an
add-on gets retries, rate limits and one trace without writing any of them. `query` is an
`EpisodeQuery`. `ctx.cache_dir` is the add-on's own cache folder (created on first use) and
`ctx.HttpError` the error `ctx.http` raises when a host cannot be reached. A file
whose name starts with `_` or `.` is not an add-on (rename one that way to turn it off without
deleting it).

Optional capabilities, each switching its feature on only when some loaded add-on offers it:
- `text_search(ctx, query) -> list[dict]`: the release picker's own-words search. `query`
  is a `TextQuery`; `query.words` is what the viewer typed, trimmed. The answer is releases like
  `episode_search`'s, and the plugin labels each with the episode it names itself. With no add-on
  offering it, the picker offers no own-words search.
- Subtitle tracks per release, two functions that count only together:
  `subtitle_tracks(ctx, query) -> dict`, `query` a `TracksQuery` naming one release this add-on
  returned (`query.release_id` is that release's `id`), answers
  `{"tracks": [track, ...], "complete": bool, "first_read": bool}`. A track is
  `{id, lang, format, name, default, forced, size, url}`: `lang` an ISO 639-2 code, `format` ASS/SSA/SRT
  (others are listed but never used), `forced` a signs-only track (its language does not count), `url`
  where its text lives (a track without one counts for its language but is never fetched).
  `complete: False` means the indexer has not read the release yet, so its languages are unknown, not
  absent; `first_read: True` means this is the first time the add-on has read the release, and only
  then do its languages count toward the learned group statistics (read it once and cache it). An
  add-on that never sets `first_read` feeds the group statistics nothing: its releases still rank on
  their verified languages, but no group learns from them.
  `subtitle_text(ctx, query) -> bytes | str` returns one track's text, `query` a `TrackTextQuery`
  (`release_id`, and `track` as `subtitle_tracks` listed it). The plugin caches the text itself.
  These feed the verified-language ranking (+60 / -40), donor subtitles, anchor retiming and the
  external-subtitle timing reference. When several add-ons returned the same release (same infohash),
  its tracks are asked of the first of them that offers both functions (the add-on whose record won
  the merge, then the others in the order they were merged), under the `id` that add-on gave it.
  With no add-on offering both, those are off and ranking uses
  title-parsed languages and learned group statistics only.
- File list per release: `file_list(ctx, query) -> dict`, `query` a `FileListQuery` naming one
  release this add-on returned (`release_id`, plus `info_hash` and `title`), answers
  `{"files": [{"name": str, "size": int | None}, ...] | None, "complete": bool}`. `name` is the file's
  path inside the torrent (or its bare name), `size` its bytes when known. `files: None` means the add-on
  does not know this release's files (unknown, as if it had no capability); `complete: True` means the list
  is the whole torrent, so a pack whose list has no file for the episode is dropped. A partial list
  (`complete` False) can still pick the episode's file, but its silence drops nothing. It feeds pack file
  choice and per-episode size. Without it, a pack's files come from the debrid provider (TorBox's cached
  list, TorBox's torrent info when no add-on offers file lists, or the provider listing the files once it adds
  the torrent).
- Show ids: `show_ids(ctx, query) -> dict`, `query` a `ShowIdsQuery` naming one release this
  add-on returned (`release_id`; `info_hash` and `title` are there for later callers, and are empty when
  asked from `subs external --release-id`, which knows only the id), answers any of
  `{"tmdb": int, "imdb": "tt…", "tvdb": int, "tvdb_season": int, "anidb": int}` for the show the release
  belongs to (a missing or null key is "not known"; `tvdb_season` 0 is TVDB's specials season). They come
  before ani.zip's in the external-subtitle (Subdl, OpenSubtitles) episode plan of
  `subs external --release-id INDEXER ID`. A value of the wrong shape is logged and ignored. Without it,
  ani.zip's ids alone are used.

Loading and calling are the two things that can go wrong in a file the plugin did not write, so
both are guarded: a file that will not load is refused with a reason (Settings shows it), and a
call that raises or runs past its time budget is logged and that indexer is skipped for this play.
Neither ever fails the resolve by itself; only no add-on loaded at all does (`require`).
"""
from __future__ import annotations

import contextlib
import copy
import dataclasses
import functools
import grp
import importlib.util
import math
import os
import pwd
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import ClassVar
from pathlib import Path
from types import MappingProxyType, ModuleType

from . import core
from . import say as saymod
from . import torznab
from .core import FILES_COMPLETE, FILES_FAILED, FILES_INCOMPLETE, FILES_UNKNOWN, Log, pack_files

SUPPORTED_API_VERSIONS = (1,)
# The stage a play or the release picker fails at when no add-on loaded. Not
# `index.search`: nothing was searched, and that stage opens the release picker, which would be empty.
NONE_STAGE = "indexers.none"
# The load line's suggest when nothing loaded, by `Registry.reading()` (a folder problem logs its own).
_LOAD_SUGGEST = {saymod.NOTHING_ADDED: "no indexer add-on in {dir} and no Torznab server; the plugin ships none",
                 saymod.NONE_LOADED: "none of the files in {dir} loaded; the refused lines say why"}
CALL_BUDGET_S = float(core.DEFAULT_SETTINGS["indexerTimeoutSeconds"])


def indexers_dir() -> Path:
    return core.CONFIG_DIR / "indexers"


@dataclass
class Indexer:
    name: str
    file: str
    module: ModuleType
    builtin: bool = False           # the built-in Torznab add-on, not a file in the folder
    servers: int = 0                # how many Torznab servers it searches (the servers file's usable entries)
    feeds: int = 0                  # how many Torznab feeds those servers are (a Prowlarr is several)

    def offers(self, capability: str) -> bool:
        return callable(getattr(self.module, capability, None))

    def offers_subtitle_tracks(self) -> bool:
        return all(self.offers(f) for f in SUBTITLE_TRACK_FUNCTIONS)


@dataclass
class Registry:
    loaded: list[Indexer] = field(default_factory=list)
    refused: list[dict] = field(default_factory=list)
    dir: str = ""
    problem: str = ""               # the folder itself is unusable; "" when it is fine or simply absent

    def find(self, name: str | None) -> Indexer | None:
        """The loaded add-on of that display name (the first, in `indexers` order), or None."""
        return next((ix for ix in self.loaded if ix.name == name), None) if name else None

    def tracks_ref(self, release: dict, skip=frozenset(), passed: list | None = None) -> ReleaseRef | None:
        """Which indexer, under which of its release ids, this release's subtitle tracks are asked of:
        the first of `returned_by(release)` whose add-on is loaded, offers both
        `subtitle_tracks` and `subtitle_text` and is not named in `skip` (the add-ons that hung in this
        command), or None. Nothing is guessed: an untagged release with no `also_from` has none. Each ref
        passed over is appended to `passed` as `{indexer, release_id, why}`, `why` one of `not_loaded`,
        `no_capability`, `hung`, for the log."""
        for ref in returned_by(release):
            ix = self.find(ref.indexer)
            why = ("not_loaded" if ix is None else "no_capability" if not ix.offers_subtitle_tracks()
                   else "hung" if ix.name in skip else None)
            if why is None:
                return ref
            if passed is not None:
                passed.append({**ref.as_dict(), "why": why})
        return None

    def status(self) -> dict:
        """What Settings shows: loaded add-ons in `indexers` order, refusals with their
        reasons, and a folder problem. JSON-safe; no module leaves this object."""
        return {"dir": self.dir, "shown": saymod.shown_path(self.dir) if self.dir else "", "problem": self.problem,
                "loaded": [{"name": i.name, "file": i.file,
                            **({"builtin": True, "servers": i.servers, "feeds": i.feeds} if i.builtin else {})}
                           for i in self.loaded],
                "refused": [dict(r) for r in self.refused]}

    def reading(self) -> str:
        """Why nothing loaded, the one place it is decided ("" when something did). A
        folder problem wins: with an unusable folder the only refusals are listed names with no file."""
        if self.loaded:
            return ""
        if self.problem:
            return saymod.FOLDER_UNUSABLE
        return saymod.NONE_LOADED if self.refused else saymod.NOTHING_ADDED


# Each add-on's own cache folder, `<plugin cache>/indexers/<add-on file stem>/`.
ADDON_CACHE = "indexers"


@dataclass
class Context:
    """What every add-on function is handed as `ctx`.

    `http` is the plugin's HTTP client (`request`, `get_capped`: retries, rate limits, one log), `log`
    the command's log, `settings` a deep, frozen copy of the general settings. `cache_dir` is the
    add-on's own cache folder, created (mode 700) the first time it is read; `HttpError` is the class
    `http` raises when a request fails for good. The same object is shared by the
    add-ons of one command, and `call` hands each one a copy bound to it (`for_addon`)."""
    http: object
    log: Log
    settings: MappingProxyType
    addon: str = ""                 # the add-on's file stem, set by `call`; names its cache folder
    cache_root: Path | None = None  # where the add-on cache folders are; None is the plugin's cache (`addons check`
                                    # passes a scratch folder it removes afterwards)
    # `Http.request` raises it when a host cannot be reached after its retries (an HTTP error status is
    # returned, not raised); `get_capped` raises it past its cap. It carries `stage` and `suggest`, and an
    # add-on that re-raises it unchanged keeps the plugin's own wording for the failure.
    HttpError: ClassVar[type] = core.StageError

    def for_addon(self, indexer: "Indexer") -> "Context":
        return dataclasses.replace(self, addon=Path(indexer.file).stem)

    @property
    def cache_dir(self) -> Path:
        """`<plugin cache>/indexers/<file stem>/`, one per add-on file, created (mode 700, logged) on first
        use; an `OSError` creating it is logged with a suggestion and re-raised. The file stem, not `NAME`: it is what the `indexers` setting names, and a folder name the loader already
        accepted (no `/`, never `.` or `..`, which start with a dot)."""
        if not self.addon:
            raise RuntimeError("ctx.cache_dir is only available inside an add-on call")
        root = self.cache_root or core.CACHE_DIR
        parent = root / ADDON_CACHE
        folder = parent / self.addon
        if folder.is_dir():
            return folder
        try:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            core.set_private(parent, 0o700)             # `parents=True` made it at the umask's mode
            core.set_private(folder, 0o700)
        except OSError as exc:
            self.log.warn("indexers", "cache_dir", result="FAIL", addon=self.addon, path=str(folder),
                          error=f"{type(exc).__name__}: {exc}",
                          suggest=f"the add-on's cache folder {folder} could not be created; check that nothing "
                                  f"else is at that path and that {root} is writable")
            raise
        self.log.debug("indexers", "cache_dir", result="PASS", addon=self.addon, path=str(folder))
        return folder


@dataclass
class EpisodeQuery:
    """What one episode search is asked for. Each add-on call gets its own copy.

    `packs_allowed` is the viewer's `allowBatches`. `pack_pass` is False on the first pass and True on
    the second, which the plugin runs only when the first found fewer than three seeded releases and
    packs are allowed. `known` holds the infohashes earlier indexers already returned in this search,
    so an add-on can stop early when it is finding nothing new. It must still return a release whose
    infohash is in `known`: the merge records that it returned it too (`also_from`), and only then can
    its subtitle tracks be asked of this add-on. `matches`, `is_pack`, `pack_by` and `reject` are
    the plugin's own title checks, for add-ons whose search is by free text rather than episode id.
    """
    ids: dict
    episode: int
    variants: list[str]
    series_title: str
    alt_numbers: list[int]
    season: int | None
    packs_allowed: bool
    pack_pass: bool = False
    known: frozenset = frozenset()
    rejected: list = field(default_factory=list)
    # The entry is a movie (ani.zip's `mappings.type` or AniList's `format`, `anizip.movie_sources`, the
    # rule the resolve's sibling check uses): a movie's release is never numbered. Added last, without an API
    # bump, so a positional construction keeps its meaning.
    movie: bool = False
    # For a movie, the ani.zip season-0 pair it is filed as (`anizip.movie_pairs`, e.g. `{(0, 10)}`
    # for a movie filed as `S00E10`), the one episode marker its own release may state. Empty otherwise. Added last,
    # as `movie` was.
    movie_pairs: frozenset = frozenset()
    # Any non-movie entry with one episode (AniList `episodes` is 1) and episode 1 is asked. Its own
    # releases rarely say 1 (`S04E30`, `Special 2`), so the episode test is not asked of them; the name has to hold
    # one of the entry's titles instead. Added last, as `movie` was.
    single_episode: bool = False
    # For a one-episode entry, `(id, title, format)` of each related show with more than one episode
    # (`anilist.related_shows`): a name that is one of their episodes is dropped although it holds the entry's title.
    # Empty otherwise, and when AniList's relations answer could not be read. Added last, as `movie` was.
    related_shows: tuple = ()
    # For a later-season entry (`cli.entry_identity`'s sequel verdict) that is neither a movie nor a one-episode entry,
    # `(id, title, format, episodes)` of each show two relation hops around it (`anilist.franchise_shows`): a single
    # whose words are exactly one of their titles is that show's episode (`franchise_show`). Empty otherwise, and when
    # AniList's answer could not be read. Added last, as `movie` was.
    franchise_shows: tuple = ()
    # ani.zip files this entry, whose titles state no season (`season` is None), as TVDB's first season:
    # its episode 1 is `S1E1`, the asked episode is paired in season 1, and ani.zip pairs no episode past TVDB season 1.
    # Not for a sequel, a movie or a one-episode entry (`cli.first_season_for`). A single stating season 2 or later is
    # then another season's episode (`matches`), unless one of the entry's titles ends in that season's number
    # (`first_season_titles`); so is a pack stating that one season. Added last, as `movie` was.
    first_season: bool = False
    # Every title of the entry, AniList's synonyms uncapped (`variants` holds two of
    # them), read only for that trailing-number keep (`cli.entry_titles`). Empty unless `first_season`. Added last.
    first_season_titles: tuple = ()
    # Every title of the entry, AniList's synonyms uncapped (`cli.entry_titles`). The show test reads
    # them beside `variants` only to keep a name it passed by spelling alone (`score.spelling_reading`): a name that
    # shares a word with none of them is another show (`Hanayomigatari 1-5` for `Kabukiyomigatari`). Added last.
    titles: tuple = ()
    # `[title, why]` of each name `matches` dropped as another show of the franchise while it states a
    # part (`Part 2 - 10`): the plugin's own record, filled whether or not the add-on hands the drop to `reject`, and
    # gathered per add-on call (each starts it empty) for the `index.search franchise_part` line. Added last.
    part_drops: list = field(default_factory=list)
    # For a one-episode entry, `(id, title, episodes)` of each one-episode prequel (the first half of a
    # split special, `anilist.prequel_shows`): a name that is a prequel's title with episode 2 is the entry's own
    # (`score.prequel_continuation`), although it states no title of the entry. Empty otherwise. Added last.
    prequel_shows: tuple = ()
    # `[title, code, why]` of each name `matches` dropped as another entry of the franchise that the
    # ambiguity guard reads (`score.guard_reads_drop`: a franchise show, a first season's other season), whether or not
    # the add-on hands it to `reject`. The guard reads every name the search returned, dropped or not.
    # An add-on that stops asking titles once a query finds nothing new reads it too, and counts such a drop as new, so
    # a drop the guard reads cannot end its search a title early. Gathered per add-on call, as `part_drops` is; a call
    # that runs out of time may leave entries whose releases are discarded (only more evidence). Added last.
    entry_drops: list = field(default_factory=list)
    # The entry is a TV series (`cli.tv_series_entry`: ani.zip's `mappings.type` or AniList's `format`
    # says TV or TV_SHORT, and neither names another format), neither a movie nor a one-episode entry. Its episode is
    # no OVA: the show test drops a single stating one before its marker (`score.ova_entry_reason`). Added last.
    tv_series: bool = False

    def matches(self, title: str) -> tuple[bool, str]:
        """For a movie, a title that passes and states an episode other than `movie_pairs` is dropped
        with `movie_episode <markers>` (`score.movie_episode_reason`): another show's `- S01E01 - <the movie's
        title>` passes the episode test for episode 1. Checked after the keep, so a title of another show still
        reads `series` and stays on the picker's rejected list. Every add-on that calls `matches` gets it.

        For a one-episode entry, a name that is not a pack and fails only the episode test is kept when
        the show and season tests pass (reason `single_episode kept …`), and any kept name that is not a pack is dropped
        when `score.single_episode_reason` names why it is not the entry (`single_episode …`). That
        reason also names a related show whose episode the name is (`related_shows`).

        For a later-season entry with `franchise_shows`, a kept name that is not a pack and whose words
        are exactly another show's title is dropped (`franchise_show …`, `score.franchise_show_reason`): the first cour's
        `[Grp] Tidewater Lantern - 01` for `Tidewater Lantern 2`, which names no season the season test could read.
        Not asked of a name that states the entry's own season (`- 01 [S2]`, `- 01 (2nd
        Season)` for a season-2 entry), which the season test has already agreed with.

        For an entry ani.zip files as TVDB's first season (`first_season`), whose titles state none, a
        kept single that states season 2 or later is dropped with `season N wanted 1 (…)` (the
        season-2 `[Grp] Show 2nd Season - 05` for season 1's episode 5), unless one of the entry's titles ends in that
        season's number (`Show 3` keeps `Show X S03E05`). A pack stating one such season is dropped too
        (`Show S2 (01-11) [Batch]`); a pack of several seasons (`S1-S2`, `Season 01-02`) is kept.

        A name the show test passes by spelling alone, sharing no word with any of the entry's titles
        (`titles` beside `variants`), is dropped as another show (`series … by spelling alone`), a pack included.

        For a later-season entry with `franchise_shows`, a single that states a part (`Part 2`, `Cour 2`)
        none of its titles state, where a franchise show is one of its titles plus that part, is that show's
        (`franchise_show …: it states Part 2 …`, `score.franchise_part`); `Part 2 - 10` is read as a single here
        (`is_pack` reads it so too). Each such drop is kept in `part_drops`.

        For a one-episode entry, a name that fails only the episode test and is one of a prequel's
        titles with the episode after that show's last (`prequel_shows`, `score.prequel_continuation`) is the entry's
        own (`single_episode kept: names its prequel …`), although it holds none of the entry's titles.

        A drop the ambiguity guard reads (`score.guard_reads_drop`) is also kept in `entry_drops`.

        For a TV series (`tv_series`), a single stating an OVA before its episode marker is another entry
        of the franchise (`franchise_show …`, `score.ova_entry_reason`): `[Grp] Tidewater Lantern Driftwood -
        OAV 01` is the OVA's episode 1, not the series'.

        The title is read cut to `core.RELEASE_TITLE_MAX`, as the plugin reads every release title."""
        from .score import guard_reads_drop
        title = core.cut_title(str(title or ""))
        ok, why = self._matches(title)
        code = why.split()[0] if why else ""
        if not ok and guard_reads_drop(code, why):
            self.entry_drops.append([title, code, why])
        return ok, why

    def _matches(self, title: str) -> tuple[bool, str]:
        """The checks `matches` documents, without its `entry_drops` record."""
        from .score import (NO_TITLE_IN_FULL, movie_episode_reason, part_numbers, prequel_reading,
                            release_matches_target, single_episode_reason)
        ok, why = release_matches_target(title, self.variants, self.episode, self.alt_numbers,
                                         target_season=self.season, titles=self.titles, tv_series=self.tv_series)
        if ok:
            reason = self.first_season_reason(title)
            if reason:
                return False, reason
        if ok and self.movie:
            reason = movie_episode_reason(title, self.movie_pairs)
            if reason:
                return False, reason
        if ok:
            reading = self.franchise_reading(title)
            if reading and reading.reason:
                if part_numbers(title):
                    self.part_drops.append([title, reading.reason])
                return False, reading.reason
        # The one-episode rules ask the title-only reading (`is_pack`), not `pack_by`: a one-episode entry's own
        # release may state its season (`Show Second Season OVA`) and must still be read as a single.
        if self.single_episode and not self.movie and not self.is_pack(title):
            if not ok and why == "episode":
                ok, why = release_matches_target(title, self.variants, self.episode, self.alt_numbers,
                                                 target_season=self.season, check_episode=False, titles=self.titles)
                why = f"single_episode kept: states no episode {self.episode}, names the show" if ok else why
                kept = prequel_reading(title, self.prequel_shows, self.episode).kept if ok else ""
                if kept:
                    return True, kept
            if ok:
                reason = single_episode_reason(title, self.variants, self.related_shows, self.alt_numbers)
                if reason == NO_TITLE_IN_FULL:       # say a prequel's own episode so
                    reason = prequel_reading(title, self.prequel_shows, self.episode).own or reason
                if reason:
                    return False, reason
        return ok, why

    @staticmethod
    def is_pack(title: str) -> bool:
        from .score import parse_title
        return bool(parse_title(title)["batch"])

    def pack_by(self, title: str) -> str:
        """Why the title reads as a pack for this entry, or "" when it does not: `batch`, `range`, or
        `season: <statement>` for a season pack of the entry's own season (`score.season_pack`, read only when
        `season` is known, and agreeing with `variants` on `Part`/`Cour`). `is_pack` is the title's words alone; the
        pack pass asks this."""
        from .score import parse_title
        return parse_title(title, self.season, self.variants)["pack_by"]

    def pack_refused(self, title: str) -> str:
        """Why a title stating this entry's season is not read as its pack (`one file`,
        `not video`, `extras`, `several shows`, `episode marker`, `several seasons`, a `Part`/`Cour` disagreement), or
        "" when it states no season or another, or is a pack."""
        from .score import parse_title
        return parse_title(title, self.season, self.variants)["pack_refused"]

    def _first_season_asked(self) -> bool:
        return self.first_season and self.season is None and not (self.movie or self.single_episode)

    def first_season_reason(self, title: str) -> str:
        """Why a name is another season's for a first-season entry (`first_season`), or "": it states
        season 2 or later, and no title of the entry ends in that season (`score.first_season_reason`). A
        pack too, unless it states several seasons (`S1-S2`). The reason starts with `season`, the code the season
        test's own drops carry, so an add-on hands it to the release picker as it does those."""
        from .score import first_season_reason
        if not self._first_season_asked():
            return ""
        return first_season_reason(title, (*self.variants, *self.first_season_titles), self.is_pack(title))

    def first_season_read(self, title: str) -> "score.FirstSeasonReading | None":
        """The stated-season gate, `score.first_season_reading` of a name stating
        season 2 or later (`score.first_season_stated`) for a first-season entry, a pack read as one (`is_pack`), or
        None (the rule is off, or the name states no such season). The one gate `first_season_kept` and
        `cli.stated_season_check` ask."""
        from .score import first_season_reading, first_season_stated
        if not self._first_season_asked():
            return None
        own = (*self.variants, *self.first_season_titles)
        stated = first_season_stated(title, own)[0]
        if stated is None or stated < 2:
            return None
        return first_season_reading(title, own, self.is_pack(title))

    def first_season_kept(self, title: str) -> str:
        """For a name the search returned that states season 2 or later,
        why it stayed for a first-season entry, or "" (the rule is off, the name states no such season, or the rule
        drops it): a title ending in that season, or a pack of several seasons. A name the rule drops
        has no keep reason; `index_search` asks this only of a
        record it kept (`cli.stated_season_check` drops the rest)."""
        reading = self.first_season_read(title)
        return reading.why if reading is not None else ""

    def franchise_reading(self, title: str, parts_only: bool = False):
        """`score.franchise_reading` of a name for this entry, or None when the rule is not asked (no
        `franchise_shows`, a movie, a one-episode entry, a pack). A name stating the entry's own season
        is kept with that reason, and every kept reading says why.

        A single named `Part 2 - 10` is read (`score.pack_range_search`, which `is_pack` reads through
        too), and the entry's every title (`titles`) is handed to the part reading. A name stating the entry's own
        season and a part a franchise show is (`Show Season 3 Part 2 - 05` for
        `Show Season 3`) is read (`score.states_own_season`).

        `parts_only` (the plugin's own reading of a first season's names, `cli.first_season_parts`) asks
        the part reading alone (`exact=False`); an add-on's `matches` never does. The part is
        read before the episode marker only (`score.stated_parts`)."""
        from .score import franchise_reading, franchise_words, pack_range_search, states_own_season, FranchiseReading
        if not self.franchise_shows or self.movie or self.single_episode or pack_range_search(title):
            return None
        if states_own_season(title, self.season, (*self.variants, *self.titles), self.franchise_shows, self.episode,
                             self.alt_numbers):
            words = franchise_words(title, self.episode, self.alt_numbers)[0]
            return FranchiseReading(None, words, f"states the entry's own season {self.season}", bool(words))
        return franchise_reading(title, self.variants, self.franchise_shows, self.episode, self.alt_numbers,
                                 self.titles, exact=not parts_only)

    def part_kept(self, title: str, indexer: str | None = None, pack_pass: bool | None = None,
                  parts_only: bool = False) -> str:
        """For a name the search returned that states a part, why it stayed for an entry with
        `franchise_shows`, or "" (no shows, or no part stated): a pack (exempt), a movie or a one-episode
        entry (not read), the reading's own why (the part is the entry's, or no franchise show is that part), or, when
        the reading would drop it, that its add-on kept it without asking `matches`. That
        reason does not guess why; it names the record's `indexer` and the `query.pack_pass` of the pass that returned
        it, as the caller knows them. `parts_only` as `franchise_reading` takes it."""
        from .score import part_numbers, pack_range_search, PACK_EXEMPT_PART
        if not self.franchise_shows or not part_numbers(title):
            return ""
        if self.movie or self.single_episode:
            return "a movie or a one-episode entry: the franchise reading is not asked"
        if pack_range_search(title):
            return PACK_EXEMPT_PART
        reading = self.franchise_reading(title, parts_only)
        if reading is None:
            return ""
        if reading.why or not reading.reason:
            return reading.why
        return ("not asked: its add-on kept it without query.matches (an episode-id search, or the add-on's own pack "
                f"pass), which would drop it; indexer {indexer or '?'}, query.pack_pass "
                f"{'?' if pack_pass is None else bool(pack_pass)}")

    def reject(self, release: dict, code: str, why: str) -> None:
        """A release that names another show or season, for the release picker's rejected list. It
        reaches the picker only if this call finishes inside its budget. One dropped as
        another show of the franchise (`franchise_show`) is kept off the picker's rows, and only lets a viewer's earlier
        pick of it be found again."""
        self.rejected.append((release, code, why))

    def for_call(self, known: frozenset) -> "EpisodeQuery":
        return dataclasses.replace(self, ids=dict(self.ids), variants=list(self.variants),
                                   alt_numbers=list(self.alt_numbers), known=known, rejected=[], part_drops=[],
                                   entry_drops=[])


@dataclass(frozen=True)
class TextQuery:
    """What one text search is asked for: the viewer's own words, trimmed, as typed. An object rather
    than a string, as `episode_search` gets an `EpisodeQuery`, so fields (the episode, the show's ids)
    can be added later without an API_VERSION bump; an add-on reads only the fields it knows. `movie`
    says the entry is a movie, by the same rule as `EpisodeQuery.movie`."""
    words: str
    movie: bool = False


@dataclass(frozen=True)
class TracksQuery:
    """Which release's subtitle tracks are asked for: `release_id` is the `id` this add-on gave the
    release in its own search answer; `info_hash` and `title` are there for an add-on that keys on
    those instead. Frozen, and an object for the same reason as `TextQuery`."""
    release_id: object
    info_hash: str = ""
    title: str = ""


@dataclass(frozen=True)
class TrackTextQuery:
    """One track's text is asked for: the release (`release_id`, as in `TracksQuery`) and the track as
    `subtitle_tracks` listed it (read-only)."""
    release_id: object
    track: MappingProxyType


@dataclass(frozen=True)
class Builtin:
    """A built-in add-on in the load order: it takes part like a file named `<stem>.py`, so it has the
    two things `_order` reads of a file, `stem` and `name`."""
    stem: str

    @property
    def name(self) -> str:
        return f"{self.stem}.py"


def _order(files: list[Path], wanted: list[str], builtin: tuple[Builtin, ...] = ()) -> tuple[list, list[str]]:
    """The `indexers` setting first, in its order; any file it does not list after, by name, so a
    file dropped into the folder works without touching a setting. Also returns the listed names
    that have no file. A built-in add-on takes part like a file named `<stem>.py`."""
    by_stem: dict = {e.stem: e for e in [*files, *builtin]}
    first = [by_stem[w] for w in wanted if w in by_stem]
    missing = [w for w in wanted if w not in by_stem]
    return first + sorted((e for e in by_stem.values() if e not in first), key=lambda e: e.name), missing


def is_addon_name(name: str) -> bool:
    """Whether the folder's reader takes a file of this name for an add-on: `*.py`, not starting with `_` or `.`."""
    return name.endswith(".py") and not name.startswith(("_", "."))


@functools.lru_cache(maxsize=8)
def _own_group(gid: int) -> bool | None:
    """Whether `gid` is a group of the user alone: no member but them, and no other account's primary group (the
    user-private group a umask of 002 is made for). An unknown group is nobody's own (False); None when the accounts
    could not be read (the user's own uid has no entry: sssd unreachable, a container's bare uid)."""
    try:
        me = pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        return None
    try:
        members = grp.getgrgid(gid).gr_mem
    except KeyError:
        return False
    except OSError:
        return None
    if set(members) - {me}:
        return False
    try:
        return not any(p.pw_gid == gid and p.pw_name != me for p in pwd.getpwall())
    except OSError:
        return None


def _not_safe(st: os.stat_result) -> str:
    """Why a folder or file another user could change is not loaded from, or "". Group-write is accepted only for a
    group that is the user's alone (`_own_group`): with a umask of 002 every file the user makes is 664."""
    mode = st.st_mode & 0o777
    if st.st_uid != os.getuid():
        return "is owned by another user"
    if mode & 0o002:
        return f"can be changed by other users (mode {mode:o})"
    if mode & 0o020:
        own = _own_group(st.st_gid)
        if own is None:
            return f"can be changed by its group, and its group could not be checked (mode {mode:o})"
        if not own:
            return f"can be changed by other users (mode {mode:o})"
    return ""


def folder_refusal(folder: Path) -> str:
    """"" when add-ons may load from `folder`; else the sentence Settings shows. The folder (where a link to it leads)
    and the one above it (which could swap it) must be the user's own and not writable by others (`_not_safe`): an
    add-on runs as the user, on every command that reads the folder. `__pycache__` is not read (`load_file`)."""
    for p in (folder, folder.parent):
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            return f"{saymod.shown_path(str(p))} could not be checked ({exc.strerror or type(exc).__name__})"
        why = _not_safe(st)
        if why:
            return (f"{saymod.shown_path(str(p))} {why}, so no add-on is loaded from it; make it yours alone "
                    f"(chmod go-w)")
    return ""


def file_refusal(f: Path, folder: Path) -> str:
    """"" when the add-on file `f` in `folder` may be loaded; else the reason, as Settings shows it: a link must lead
    to a file inside the folder, and the file must be the user's own and not writable by group or others."""
    try:
        real, base = f.resolve(), folder.resolve()
        if f.is_symlink() and not real.is_relative_to(base):
            return (f"is a link to {saymod.shown_path(str(real))}, outside {saymod.shown_path(str(folder))}; copy the "
                    "file into the folder instead")
        why = _not_safe(real.stat())
    except OSError as exc:
        return f"could not be checked ({exc.strerror or type(exc).__name__})"
    return f"{why}, so it is not loaded; make it yours alone (chmod go-w)" if why else ""


def load_file(f: Path) -> tuple[Indexer | None, str]:
    """One add-on file, loaded as the registry loads it: `(Indexer, "")`, or `(None, reason)` with the sentence
    Settings shows. `load` calls it for each file in the folder, and `addons check` for one file anywhere."""
    mod_name = "omarchy_anime_indexer_" + re.sub(r"\W", "_", f.stem)
    try:
        spec = importlib.util.spec_from_file_location(mod_name, f)
        module = importlib.util.module_from_spec(spec)
        # Compiled from the file itself, never through `__pycache__`: Python would run a cached copy whose stamp
        # matches the file (one another user could plant), and would write one at the user's umask.
        code = compile(f.read_bytes(), str(f), "exec", dont_inherit=True)
        # stdout is the CLI's JSON answer (`accounts`, `resolve`): a stray print at import must
        # not corrupt it, and a `sys.exit()` there must refuse the file, not end the command.
        with contextlib.redirect_stdout(sys.stderr):
            exec(code, module.__dict__)                 # noqa: S102 - the add-on is the user's own code
    except (Exception, SystemExit) as exc:              # noqa: BLE001 - any error in a file we did not write
        return None, f"could not be loaded: {type(exc).__name__}: {exc}"
    version = getattr(module, "API_VERSION", None)
    if version not in SUPPORTED_API_VERSIONS:
        declared = "declares no API_VERSION" if version is None else f"API_VERSION {version!r} is not supported"
        return None, f"{declared} (this plugin supports {', '.join(map(str, SUPPORTED_API_VERSIONS))})"
    if not callable(getattr(module, "episode_search", None)):
        return None, "has no episode_search"
    return Indexer(name=str(getattr(module, "NAME", "") or f.stem), file=f.name, module=module), ""


def load(settings: dict, log: Log | None = None) -> Registry:
    folder = indexers_dir()
    reg = Registry(dir=str(folder))
    problem = ""
    error = ""                                          # the OSError itself, for the log only
    files: list[Path] = []
    # `problem` and the refused reasons are sentences the Settings sheet shows, so the
    # folder in them is `~/…` as the viewer reads it, and they carry `strerror` rather than the error, whose text
    # repeats the absolute path. `dir` keeps the absolute path, and the `indexers load` FAIL line keeps the error.
    shown = saymod.shown_path(str(folder))
    if folder.exists() and not folder.is_dir():
        problem = f"{shown} exists but is not a folder; add-ons go in a folder of that name"
    elif folder.is_dir():
        try:
            files = [f for f in folder.iterdir() if is_addon_name(f.name) and f.is_file()]
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"
            problem = f"{shown} could not be read ({exc.strerror or type(exc).__name__}); check its permissions"
        unsafe = folder_refusal(folder) if not problem else ""
        if unsafe:
            problem, error, files = unsafe, "not the user's own, or writable by others", []
    # The built-in Torznab add-on, only when the servers file lists a usable server. With no file or an
    # empty list nothing is logged or refused here, so the registry is exactly what it was without Torznab.
    servers = torznab.load_feeds()
    builtin = (Builtin(torznab.STEM),) if servers.sources else ()
    if servers.problem:
        reg.refused.append({"file": torznab.SERVERS_FILE, "reason": servers.problem})
    ordered, missing = _order([f for f in files if not (builtin and f.stem == torznab.STEM)],
                              [str(w) for w in core.as_list(settings.get("indexers"), [])], builtin)
    if builtin and any(f.stem == torznab.STEM for f in files):
        reg.refused.append({"file": f"{torznab.STEM}.py", "reason": "the name torznab is the built-in Torznab "
                            "add-on's while a Torznab server is set up; rename the file"})
    for name in missing:
        reg.refused.append({"file": f"{name}.py", "reason": f"listed in the indexers setting, but no such file "
                                                            f"in {shown}"})
    for f in ordered:
        if isinstance(f, Builtin):                      # the built-in add-on
            reg.loaded.append(Indexer(name=torznab.NAME, file=torznab.STEM, module=torznab.addon(servers.sources),
                                      builtin=True, servers=len(servers.servers), feeds=len(servers.feeds)))
            continue
        unsafe = file_refusal(f, folder)
        if unsafe:
            reg.refused.append({"file": f.name, "reason": unsafe})
            if log is not None:
                log.warn("indexers", "unsafe", result="FAIL", file=f.name, reason=unsafe,
                         suggest="an add-on runs as you: one another user can change is not loaded")
            continue
        indexer, reason = load_file(f)
        if indexer is None:
            reg.refused.append({"file": f.name, "reason": reason})
            continue
        reg.loaded.append(indexer)
    reg.problem = problem
    if log is not None:
        if problem:
            log.warn("indexers", "load", result="FAIL", dir=str(folder), error=error or None, suggest=problem)
        else:
            log.info("indexers", "load", result="PASS" if reg.loaded else "EMPTY", dir=str(folder),
                     loaded=[i.name for i in reg.loaded], refused=reg.refused,
                     suggest=_LOAD_SUGGEST.get(reg.reading(), "").format(dir=folder))
        if servers.sources or servers.skipped:
            log.info("indexers", "torznab", result="PASS" if servers.sources else "FAIL", servers=len(servers.servers),
                     feeds=len(servers.feeds), labels=servers.labels, records=servers.records,
                     skipped=servers.skipped,
                     suggest=(f"fix the skipped servers in {servers.path}" if servers.skipped else
                              "a server's remembered detection could not be read; it is detected again on its "
                              "next play" if any(r["record"] == "unreadable" for r in servers.records) else ""))
        for r in reg.refused:
            where = servers.path if r["file"] == torznab.SERVERS_FILE else folder / r["file"]
            log.warn("indexers", "refused", result="FAIL", **r, suggest=f"fix or remove {where}")
    return reg


def own_failure(exc: Exception, kind: str, **detail) -> Exception:
    """`call`'s own failure (`kind` "timeout" or "wrong_type"), marked as `call_failure` so `addons check` tells it
    from the same error raised by the add-on, with what `call` knew in `call_detail` (the type returned, the one
    wanted). A mark, not a subclass: `failed` names the class to the viewer."""
    exc.call_failure = kind
    exc.call_detail = detail
    return exc


def call(indexer: Indexer, capability: str, log: Log, ctx: Context, *args, budget_s: float = CALL_BUDGET_S,
         about: dict | None = None, failures: list | None = None, expect: type | tuple = list,
         summary=None, cut: str | None = None):
    """Run one add-on function under a time budget. Returns its result, or None when it raised,
    timed out or returned something that is not an `expect` (a list of releases, unless the capability
    answers otherwise); each of those is logged with the reason. `summary(value) -> (result, fields)`
    says how a good answer reads in the log; by default PASS/EMPTY and `count=len(value)`.
    `about` names what was asked (episode, pass) on every log line of the call; `failures`, when
    given, gets `(indexer, exception or None)` for each failure so the caller can say why nothing
    was found. `cut` is what a timeout's `suggest` says when `budget_s` is a caller's deadline, not the
    add-on's own budget: the caller knows whose deadline it was and whether the add-on is now skipped.

    The call runs on a daemon thread: Python cannot stop a thread, so a hung add-on is abandoned,
    its late result discarded, and the play goes on without it.
    """
    about = about or {}
    box: dict = {}

    own = ctx.for_addon(indexer) if isinstance(ctx, Context) else ctx

    def run():
        try:
            box["value"] = getattr(indexer.module, capability)(own, *args)
        except BaseException as exc:                        # noqa: BLE001
            box["error"] = exc

    t0 = time.monotonic()
    worker = threading.Thread(target=run, name=f"indexer-{indexer.file}", daemon=True)
    worker.start()
    worker.join(budget_s)
    ms = (time.monotonic() - t0) * 1000
    if worker.is_alive():
        log.warn("indexers", capability, result="FAIL", indexer=indexer.name, ms=ms, budget_s=round(budget_s, 2),
                 **about, suggest=cut or f"{indexer.file} ran past its {budget_s:g} s budget and is skipped for this "
                                         f"play; raise indexerTimeoutSeconds or fix the add-on")
        if failures is not None:
            failures.append((indexer, own_failure(TimeoutError(f"{indexer.name} did not answer within "
                                                               f"{budget_s:g} s"), "timeout")))
        return None
    if "error" in box:
        exc = box["error"]
        log.warn("indexers", capability, result="FAIL", indexer=indexer.name, ms=ms, **about,
                 error=f"{type(exc).__name__}: {exc}",
                 suggest=f"{indexer.file} failed and is skipped for this play; its own log lines above say why")
        if failures is not None:
            failures.append((indexer, exc))
        return None
    value = box.get("value")
    if not isinstance(value, expect):
        wanted = " or ".join(t.__name__ for t in (expect if isinstance(expect, tuple) else (expect,)))
        log.warn("indexers", capability, result="FAIL", indexer=indexer.name, ms=ms, **about,
                 returned=type(value).__name__,
                 suggest=f"{indexer.file} must return a list of releases" if expect is list
                 else f"{indexer.file}'s {capability} must return a {wanted}")
        if failures is not None:
            failures.append((indexer, own_failure(TypeError(f"{indexer.name} returned {type(value).__name__}, not a "
                                                            f"{wanted}"), "wrong_type", returned=type(value).__name__,
                                                 wanted=wanted)))
        return None
    result, fields = summary(value) if summary else (("PASS" if value else "EMPTY"), {"count": len(value)})
    # A summary may read an answer as partly or wholly unusable (show ids): that is a warning, not news.
    emit = log.warn if result in ("PARTIAL", "FAIL") else log.info
    emit("indexers", capability, result=result, indexer=indexer.name, ms=ms, **fields, **about)
    return value


def failed(failures: list) -> core.StageError:
    """The error for "nothing found, and these add-ons failed". "No releases" would send the
    viewer to check whether the episode aired; the failure is what they need to see. One failure keeps
    its own words (an unreachable host names itself); several are listed."""
    first = failures[0][1]
    if len(failures) == 1 and isinstance(first, core.StageError):
        return core.StageError("index.search", str(first), first.suggest)
    names = ", ".join(f"{ix.name} ({type(exc).__name__}: {exc})" for ix, exc in failures)
    return core.StageError("index.search", f"no releases, and these indexers failed: {names}",
                           "the log's indexers lines say why each failed; try again in a minute")


def require(reg: Registry, log: Log) -> None:
    """A play or the picker's list with no add-on loaded stops here, before any search,
    with its own stage and words. It used to run on to "no releases found" and tell the viewer to
    check whether a 2011 episode had aired. Three readings: nothing added, files added and none
    loaded, or the folder unusable. The sentences are `say.no_indexer`'s; the detail is the log's."""
    reading = reg.reading()
    if not reading:
        return
    sentences = saymod.no_indexer(reg.dir, reading, len(reg.refused),
                                  torznab=any(r.get("file") == torznab.SERVERS_FILE for r in reg.refused))
    detail = {saymod.FOLDER_UNUSABLE: f"folder problem: {reg.problem}",
              saymod.NONE_LOADED: "not loaded: " + "; ".join(f"{r['file']} ({r['reason']})" for r in reg.refused),
              saymod.NOTHING_ADDED: f"no add-on in {reg.dir} and no Torznab server; the plugin ships none"}[reading]
    log.warn(NONE_STAGE, "require", result="FAIL", dir=reg.dir, reading=reading, refused=len(reg.refused),
             problem=reg.problem, reason="no indexer add-on loaded, so nothing can be searched",
             suggest=f"add a Torznab server in Accounts, or put an indexer add-on (.py) in {reg.dir}"
             if reading == saymod.NOTHING_ADDED
             else "the indexers load and refused lines say why each file did not load")
    # `suggest` is what every viewer surface shows (the panel, the picker, the player overlay appends
    # it), so it is the action sentence alone; the files and their reasons ride in `detail` (the log).
    raise core.StageError(NONE_STAGE, sentences["headline"], sentences["action"], say=sentences, detail=detail,
                          dir=reg.dir, refused=[dict(r) for r in reg.refused], problem=reg.problem)


def _frozen(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _frozen(v) for k, v in value.items()})
    if isinstance(value, (list, tuple, set)):
        return tuple(_frozen(v) for v in value)
    return value


def context(http, log: Log, settings: dict, cache_root: Path | None = None) -> Context:
    """Settings reach an add-on as a deep, frozen copy: nothing it does can change the play's own. `cache_root` puts
    the add-ons' cache folders somewhere else than the plugin's cache (`addons check`'s scratch folder)."""
    return Context(http=http, log=log, settings=_frozen(copy.deepcopy(dict(settings))), cache_root=cache_root)


def budget(settings: dict) -> float:
    """`indexerTimeoutSeconds`. The settings form clamps it to whole seconds (5-300); a fraction is
    accepted here only because nothing below 0.1 s could ever finish."""
    try:
        value = float(settings.get("indexerTimeoutSeconds", CALL_BUDGET_S))
    except (TypeError, ValueError):
        return CALL_BUDGET_S
    return value if value >= 0.1 else CALL_BUDGET_S


# ----------------------------------------------------------------------------- subtitle tracks

SUBTITLE_TRACK_FUNCTIONS = ("subtitle_tracks", "subtitle_text")
SUBTITLE_FORMATS = ("ASS", "SSA", "SRT")
# A track's text, whoever serves it: 12 MiB admits the 7.46 MB `[ESTOCS-3BSQ]` ASS (the only Arabic source
# for one Thistle episode) and refuses the 21.9 MB `[Kome]` karaoke file.
MAX_PLAIN_BYTES = 12 * 2**20
# One track's text. An add-on was measured reading a track in 0.3-1.3 s (2026-09-16) under its own
# 1.5 s socket timeout; a hung add-on must not hold a play for the whole `indexerTimeoutSeconds`.
TRACK_TEXT_BUDGET_S = 10.0


@dataclass(frozen=True)
class ReleaseRef:
    """One release as the plugin names it across indexers: the indexer's name and that indexer's own
    release id. `as_dict()` is the one spelling it travels in (donor records, `meta.json`, the worker's
    `--reference-releases`)."""
    indexer: str | None
    release_id: object

    def as_dict(self) -> dict:
        return {"indexer": self.indexer, "release_id": self.release_id}


def release_key(r: dict) -> ReleaseRef:
    """The `ReleaseRef` of a release record (its `indexer` tag and the `id` its add-on gave it) or of a
    reference (`{indexer, release_id}`). Two add-ons may number their releases alike, so a release is
    never matched on its id alone."""
    rid = r["release_id"] if "release_id" in r else r.get("id")
    return ReleaseRef(str(r.get("indexer")) if r.get("indexer") else None, rid)


def returned_by(r: dict) -> list[ReleaseRef]:
    """Every indexer that returned this release, each under its own release id: the record's own
    `release_key` (the indexer whose record won the merge), then its `also_from`, the other indexers that
    returned the same infohash in the order they were merged. That is the `indexers` order within one pass,
    but the pack pass can make a later indexer the winner, and a stored search keeps the order it ran with.
    Only tagged refs with an id count. A record without `also_from` (a pin, a search stored before
    `also_from` existed, a reference) has its own key alone."""
    out: list[ReleaseRef] = []
    seen = RefSet()
    for ref in [release_key(r)] + [release_key(a) for a in (r.get("also_from") or []) if isinstance(a, dict)]:
        if ref.indexer and ref.release_id is not None and ref not in seen:
            out.append(ref)
            seen.add(ref)
    return out


class RefSet:
    """`ReleaseRef`s a merge has seen, tested in constant time (a list was tested ref by ref, quadratic over a hostile
    answer's thousands of ids). A release id is any value an add-on gave, so one that cannot be hashed (a list,
    an object) is kept in a list beside the set and tested as a list would."""

    def __init__(self, refs=()):
        self._hashed: set = set()
        self._other: list = []
        for ref in refs:
            self.add(ref)

    def add(self, ref: ReleaseRef) -> None:
        try:
            self._hashed.add(ref)
        except TypeError:
            self._other.append(ref)

    def __contains__(self, ref: ReleaseRef) -> bool:
        try:
            return ref in self._hashed
        except TypeError:
            return ref in self._other


def tracks_key(r: dict) -> ReleaseRef:
    """The release its subtitle tracks were read from (`tracks_from`, stamped where a `SubtitleTracks.read`
    answer is taken onto a record), else its own key. A track id is that indexer's, so everything that pairs
    a track with a release (a donor record, an anchor fetch, the raw cache path) names this ref."""
    got = r.get("tracks_from")
    return release_key(got) if isinstance(got, dict) and got.get("indexer") else release_key(r)


def _track(t: dict) -> dict:
    """One track as the plugin reads it, whatever the add-on wrote: every key present and typed."""
    size = t.get("size")
    return {"id": t.get("id"), "lang": str(t.get("lang") or "und").lower(),
            "format": str(t.get("format") or "").upper(), "name": str(t.get("name") or ""),
            "default": bool(t.get("default")), "forced": bool(t.get("forced")),
            "size": size if isinstance(size, (int, float)) and not isinstance(size, bool) else None,
            "url": str(t.get("url") or "")}


def _tracks_summary(answer: dict) -> tuple[str, dict]:
    """How a `subtitle_tracks` answer reads in the log: an unread release is INCOMPLETE, not PASS."""
    tracks = answer.get("tracks")
    n = len(tracks) if isinstance(tracks, list) else 0
    if not bool(answer.get("complete", True)):
        return "INCOMPLETE", {"complete": False, "count": n}
    return ("PASS" if n else "EMPTY"), {"count": n}


def _text_summary(text) -> tuple[str, dict]:
    return ("PASS" if text else "EMPTY"), {"bytes": len(text)}


class CapabilityHandle:
    """One optional capability for one command: which loaded add-ons offer it, which
    add-on a release is asked of (the one that returned it, `release_key`, and no other; never an untagged
    release; `SubtitleTracks` also looks at the later indexers that returned it), and the
    guarded call. Build one per command and pass it along; the load snapshot
    (`Registry`) holds no per-command state.

    Hung rule, the same for every capability: an add-on whose call runs past **its own** budget
    (`indexerTimeoutSeconds`, or the capability's `default_s`) is not asked again by this object, logged
    once as `indexers hung SKIP`; otherwise a hung add-on would cost one whole budget per release. A
    timeout at a **caller's** shorter deadline (`budget_s` passed, as the donor sweep does) does not count:
    that is the caller's budget running out, not the add-on hanging. This is the subtitle tracks'
    rule; file lists always run on their own budget, so for them it is the rule they first had.
    Unless the caller says it does (`hangs`): the track check's deadline is never under `score.READ_FLOOR_S`,
    longer than any read measured, so a read cut there is a hung add-on, and it costs the play once, not once
    more at the next own-budget call. `deadline_of` names whose deadline it was, for the log."""

    def __init__(self, reg: Registry | None, http, log: Log, settings: dict, functions: tuple[str, ...],
                 label: str):
        self.log = log
        self.reg = reg if reg is not None else Registry()
        self.functions = functions
        self.label = label                          # "subtitle-track", "file-list": the log's words
        self.hung: set[str] = set()                 # add-ons that ran past their own budget in this command
        self.ctx = context(http, log, settings)
        self.budget_s = budget(settings)

    def offers(self, ix: Indexer) -> bool:
        return all(ix.offers(f) for f in self.functions)

    @property
    def offered(self) -> bool:
        return bool(self.names)

    @property
    def names(self) -> list[str]:
        return [ix.name for ix in self.reg.loaded if self.offers(ix)]

    def target(self, release: dict) -> tuple[Indexer, ReleaseRef] | None:
        """The add-on to ask about this release and the release's key, or None: no `indexer` tag (a pin,
        or a search stored before releases were tagged), no release id, an indexer without the
        capability, or one that hung earlier in this command."""
        ref = release_key(release)
        ix = self.reg.find(ref.indexer)
        if ref.release_id is None or ix is None or not self.offers(ix) or ix.name in self.hung:
            return None
        return ix, ref

    def indexer_of(self, release: dict) -> Indexer | None:
        found = self.target(release)
        return found[0] if found else None

    def can_read(self, release: dict) -> bool:
        return self.target(release) is not None

    def _call(self, ix: Indexer, function: str, query, budget_s: float | None, default_s: float, about: dict,
              expect, summary, deadline_of: str = "", hangs: bool = False):
        failures: list = []
        own = budget_s is None
        limit = default_s if own else max(0.1, budget_s)
        hangs = own or hangs
        whose = "what was left of " + (deadline_of or "the caller's deadline")
        cut = None if own else (
            f"{ix.file} did not answer within {limit:.2f} s, {whose}; "
            + ("a read that long is a hung add-on, so it is skipped for this play; check the add-on and its site"
               if hangs else "not skipped: the deadline was the caller's, not the add-on's"))
        value = call(ix, function, self.log, self.ctx, query, budget_s=limit,
                     about=about, failures=failures, expect=expect, summary=summary, cut=cut)
        if hangs and any(isinstance(exc, TimeoutError) for _, exc in failures):
            self.hung.add(ix.name)
            self.log.warn("indexers", "hung", result="SKIP", indexer=ix.name, capability=function,
                          budget_s=default_s if own else round(limit, 2), **about,
                          suggest=f"{ix.file} did not answer within {default_s:g} s; no more {self.label} "
                                  f"calls to it for this command (raise indexerTimeoutSeconds or fix the add-on)"
                          if own else f"{ix.file} did not answer within {limit:.2f} s, {whose}; no more "
                                      f"{self.label} calls to it for this command (check the add-on and its site)")
        return value


class SubtitleTracks(CapabilityHandle):
    """The subtitle-track capability for one command: its tracks, one track's text. Asked only when the
    add-on offers both functions (`target`, `Registry.tracks_ref`). `args.track_source` in the resolve
    and play path; `None` in its place means the capability is off. Per release (up to `subtitleCheckCount`
    in a row, then the donor sweep), hence the hung rule of `CapabilityHandle`."""

    def __init__(self, reg: Registry | None, http, log: Log, settings: dict):
        super().__init__(reg, http, log, settings, SUBTITLE_TRACK_FUNCTIONS, "subtitle-track")

    def target(self, release: dict, passed: list | None = None, skip=frozenset()) -> tuple[Indexer, ReleaseRef] | None:
        """The first indexer that returned the release (`returned_by`: the one whose record won
        the merge, then the others) whose add-on is loaded, offers both functions and has not hung in this
        command, with that indexer's own release id (`Registry.tracks_ref`). Only subtitle tracks look past
        the record's own indexer; file lists and show ids are asked of it alone. `skip` names more add-ons to pass
        over (the track check's failing ones), as the hung ones are."""
        ref = self.reg.tracks_ref(release, skip=self.hung | set(skip), passed=passed)
        return (self.reg.find(ref.indexer), ref) if ref is not None else None

    def read(self, release: dict, budget_s: float | None = None, *, skip=frozenset(), deadline_of: str = "",
             hangs: bool = False) -> dict | None:
        """`{status, tracks, languages, first_read, read_from}` for one release (a record or a
        `{indexer, release_id}` reference), or None when no loaded add-on can be asked about it. `status`
        is "complete", "incomplete" (the indexer has not read it yet: `languages` None, unknown) or
        "failed" (the call raised, timed out or answered junk; logged). `languages` leaves out forced
        (signs-only) tracks. `read_from` is the `{indexer, release_id}` asked (`target`); a caller that takes
        the tracks onto a record stamps it there as `tracks_from` (`tracks_key`). `budget_s` is a caller's deadline,
        `deadline_of` whose it is and `hangs` whether a read cut at it marks the add-on hung (`CapabilityHandle`);
        `skip` names add-ons not to ask (`target`)."""
        passed: list = []
        found = self.target(release, passed, skip)
        if found is None:
            if release.get("also_from"):
                self.log.debug("indexers", "tracks_from", result="SKIP", skipped=passed,
                               reason="no indexer that returned this release can be asked for its subtitle tracks")
            return None
        ix, ref = found
        rid = ref.release_id
        if ref != release_key(release):
            self.log.debug("indexers", "tracks_from", result="PASS", indexer=ix.name, release_id=rid, skipped=passed,
                           reason="an earlier indexer that returned this release could not be asked (see skipped); "
                                  "this one returned it too")
        query = TracksQuery(release_id=rid, info_hash=str(release.get("info_hash") or ""),
                            title=str(release.get("title") or ""))
        answer = self._call(ix, "subtitle_tracks", query, budget_s, self.budget_s, {"release_id": rid}, dict,
                            _tracks_summary, deadline_of, hangs)
        failed = {"status": "failed", "tracks": [], "languages": None, "first_read": False, "read_from": ref.as_dict()}
        if answer is None:
            return failed
        complete = bool(answer.get("complete", True))
        if complete and not isinstance(answer.get("tracks"), list):
            # "Read, and no track" is `[]`; anything else would score the release -40 on no evidence.
            self.log.warn("indexers", "subtitle_tracks", result="FAIL", indexer=ix.name, release_id=rid,
                          returned=type(answer.get("tracks")).__name__,
                          suggest=f"{ix.file}'s subtitle_tracks must answer a list under \"tracks\" "
                                  f"(empty when the release has none)")
            return failed
        tracks = [_track(t) for t in answer["tracks"] if isinstance(t, dict)] if complete else []
        return {"status": "complete" if complete else "incomplete", "tracks": tracks,
                "languages": [t["lang"] for t in tracks if not t["forced"]] if complete else None,
                "first_read": answer.get("first_read") is True, "read_from": ref.as_dict()}

    def text(self, release: dict, track: dict, budget_s: float | None = None) -> bytes | str | None:
        """One track's text, as the add-on returned it (bytes or str), or None (logged by `call`).
        `release` is a record or a `{indexer, release_id}` reference."""
        found = self.target(release)
        if found is None:
            return None
        ix, ref = found
        query = TrackTextQuery(release_id=ref.release_id, track=_frozen(_track(track)))
        return self._call(ix, "subtitle_text", query, budget_s, min(self.budget_s, TRACK_TEXT_BUDGET_S),
                          {"release_id": ref.release_id, "track_id": track.get("id")}, (bytes, str), _text_summary)


# ----------------------------------------------------------------------------- file lists

FILE_LIST_FUNCTION = "file_list"


@dataclass(frozen=True)
class FileListQuery:
    """Which release's files are asked for: `release_id` is the `id` this add-on gave the release in its
    own search answer; `info_hash` and `title` are there for an add-on that keys on those instead. Frozen,
    and an object for the same reason as `TextQuery`."""
    release_id: object
    info_hash: str = ""
    title: str = ""


def _files_summary(answer: dict) -> tuple[str, dict]:
    """How a `file_list` answer reads in the log: an unknown list is UNKNOWN and a partial one INCOMPLETE,
    not PASS."""
    files = answer.get("files")
    if not isinstance(files, list):
        return "UNKNOWN", {"count": 0}
    if answer.get("complete") is not True:
        return "INCOMPLETE", {"complete": False, "count": len(files)}
    return ("PASS" if files else "EMPTY"), {"count": len(files)}


class FileLists(CapabilityHandle):
    """The file-list capability for one command: a release's files. `args.file_source`;
    `None` in its place means the capability is off."""

    def __init__(self, reg: Registry | None, http, log: Log, settings: dict):
        super().__init__(reg, http, log, settings, (FILE_LIST_FUNCTION,), "file-list")

    def read(self, release: dict) -> dict | None:
        """`{status, files}` for one release, or None when no loaded add-on can be asked about it.
        `status` is `FILES_COMPLETE` (`files` is the whole torrent: a pack without the episode can be
        dropped), `FILES_INCOMPLETE` (`files` is part of the list: a file found in it can be picked, a file
        missing from it proves nothing), `FILES_UNKNOWN` (the add-on does not know; `files` None) or
        `FILES_FAILED` (the call raised, timed out or answered junk; logged; `files` None)."""
        found = self.target(release)
        if found is None:
            return None
        ix, ref = found
        rid = ref.release_id
        query = FileListQuery(release_id=rid, info_hash=str(release.get("info_hash") or ""),
                              title=str(release.get("title") or ""))
        answer = self._call(ix, FILE_LIST_FUNCTION, query, None, self.budget_s, {"release_id": rid}, dict,
                            _files_summary)
        if answer is None:
            return {"status": FILES_FAILED, "files": None}
        raw = answer.get("files")
        if raw is not None and not isinstance(raw, list):
            self.log.warn("indexers", FILE_LIST_FUNCTION, result="FAIL", indexer=ix.name, release_id=rid,
                          returned=type(raw).__name__,
                          suggest=f"{ix.file}'s file_list must answer a list under \"files\", or None when it "
                                  f"does not know the release's files")
            return {"status": FILES_FAILED, "files": None}
        files = pack_files(raw, self.log, "indexers", indexer=ix.name, release_id=rid)
        if not files:
            # A torrent has at least one file: an empty list is no evidence the episode is absent.
            return {"status": FILES_UNKNOWN, "files": None}
        return {"status": FILES_COMPLETE if answer.get("complete") is True else FILES_INCOMPLETE, "files": files}


# ----------------------------------------------------------------------------- show ids

SHOW_IDS_FUNCTION = "show_ids"
# What a `show_ids` answer may carry. `tvdb_season` is the TVDB season the release belongs to (0 = specials),
# a hint for numbering when ani.zip has no entry for the episode, not a fact (a franchise batch says 1).
SHOW_ID_KEYS = ("tmdb", "imdb", "tvdb", "tvdb_season", "anidb")
_IMDB = re.compile(r"tt\d{5,10}")


@dataclass(frozen=True)
class ShowIdsQuery:
    """Which release's show ids are asked for: `release_id` is the `id` this add-on gave the release in its
    own search answer (from `subs external --release-id INDEXER ID` it arrives as the text typed). `info_hash`
    and `title` are empty from `subs external --release-id`, the only caller today (it knows the id alone), so
    an add-on must answer from `release_id`; they are filled only if a later caller has the release record.
    Frozen, and an object for the same reason as `TextQuery`."""
    release_id: object
    info_hash: str = ""
    title: str = ""


def _pos_int(value) -> int | None:
    """A positive int, or a string of one ("40053"); anything else (a bool, 0, junk) is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        n = int(value.strip())
        return n if n > 0 else None
    return None


def normalize_show_ids(answer: dict) -> tuple[dict, list[str]]:
    """An add-on's `show_ids` answer as the plugin uses it: `{tmdb, imdb, tvdb, tvdb_season, anidb}`, each a
    checked value or None, and the keys whose value was present but refused. tmdb/tvdb/anidb are positive ints
    (a digit string is accepted), imdb matches `tt` + 5-10 digits, tvdb_season is an int >= 0 (not a bool).
    Unknown keys are ignored."""
    out: dict = {}
    refused: list[str] = []
    for key in SHOW_ID_KEYS:
        raw = answer.get(key)
        if key == "imdb":
            value = raw.strip() if isinstance(raw, str) and _IMDB.fullmatch(raw.strip()) else None
        elif key == "tvdb_season":
            value = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None
        else:
            value = _pos_int(raw)
        out[key] = value
        if value is None and raw not in (None, ""):
            refused.append(key)
    return out, refused


# The release keys the plugin compares and does arithmetic on, outside the guarded add-on call.
RELEASE_NUMBER_KEYS = ("seeders", "leechers", "total_size", "num_files", "timestamp")
_HEX40 = re.compile(r"[0-9a-fA-F]{40}")


# The largest number a release field is read as: `NUMBER_DIGITS_MAX` digits.
_RELEASE_NUMBER_MAX = 10 ** core.NUMBER_DIGITS_MAX


def _release_number(value) -> tuple[object, str]:
    """`(value, what)` for one number field: an int, a finite float (not a bool) and None are kept (`what` ""); a string
    of ASCII digits, whitespace around it allowed, is its int ("coerced"); anything else is None ("refused"). NaN and
    infinity are refused: the ranking's `int()` raises on them (`json.loads` reads `NaN` and `Infinity`). So is a value
    of more than `NUMBER_DIGITS_MAX` digits: `int()` raises on a string of over 4,300, and a float of an int over
    10^308."""
    if value is None:
        return value, ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ok = math.isfinite(value) and abs(value) < _RELEASE_NUMBER_MAX
        return (value, "") if ok else (None, "refused")
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        if len(value.strip()) > core.NUMBER_DIGITS_MAX:
            return None, "refused"
        return int(value.strip()), "coerced"
    return None, "refused"


def normalize_releases(found: list) -> tuple[list[dict], dict]:
    """An add-on's releases as the plugin reads them, and what was changed. A release is dropped when it
    is not a dict, its `title` is not a non-empty str or its `info_hash` not 40 hex characters (docs/addons.md:
    both required). In a kept one each `RELEASE_NUMBER_KEYS` value present is `_release_number`'s; every other key, and
    the value of one that is fine, is left as the add-on gave it, so a well-formed release (the built-in Torznab
    add-on's) is the same object, unchanged. A title over `RELEASE_TITLE_MAX` is cut to it (`cut`, `longest_title`).
    The report counts `dropped` by reason, `coerced`, `refused` and `cut` by key, with one example value per key
    (`examples`, cut to 40 characters); empty when nothing was changed."""
    kept: list[dict] = []
    report: dict = {"dropped": {}, "coerced": {}, "refused": {}, "cut": {}, "examples": {}}

    def note(kind: str, key: str, value) -> None:
        report[kind][key] = report[kind].get(key, 0) + 1
        report["examples"].setdefault(key, repr(value)[:40])
    for r in found:
        if not isinstance(r, dict):
            note("dropped", "not_a_dict", r)
            continue
        title, info_hash = r.get("title"), r.get("info_hash")
        if not (isinstance(title, str) and title.strip()):
            note("dropped", "title", title)
            continue
        if not (isinstance(info_hash, str) and _HEX40.fullmatch(info_hash)):
            note("dropped", "info_hash", info_hash)
            continue
        if len(title) > core.RELEASE_TITLE_MAX:
            note("cut", "title", title)
            report["longest_title"] = max(report.get("longest_title", 0), len(title))
            r["title"] = core.cut_title(title)
        for key in RELEASE_NUMBER_KEYS:
            if key in r:
                value, what = _release_number(r[key])
                if what:
                    note(what, key, r[key])
                    r[key] = value
        kept.append(r)
    return kept, {k: v for k, v in report.items() if v}


def _show_ids_reading(file: str, answer: dict) -> tuple[str, dict]:
    """How a `show_ids` answer reads in the log, in one line: which ids came back (PASS), none (EMPTY), some
    values refused beside usable ones (PARTIAL), or only refused values (FAIL). `refused` maps each refused key to
    the value given, shortened."""
    ids, refused = normalize_show_ids(answer)
    got = {k: v for k, v in ids.items() if v is not None}
    fields: dict = {"ids": got, "refused": {k: str(answer.get(k))[:40] for k in refused}}
    if refused:
        fields["suggest"] = (f"{file}'s show_ids answered values the plugin does not accept (tmdb/tvdb/anidb positive "
                             f"ints, imdb tt + digits, tvdb_season an int >= 0); those are ignored")
        return ("PARTIAL" if got else "FAIL"), fields
    return ("PASS" if got else "EMPTY"), fields


class ShowIds(CapabilityHandle):
    """The show-id capability for one command: the show ids an indexer knows for one of its
    releases (TMDB, IMDB, TVDB and its season, AniDB). They come before ani.zip's in the external-subtitle
    episode plan. Asked of the indexer that returned the release (`release_key`), never of another."""

    def __init__(self, reg: Registry | None, http, log: Log, settings: dict):
        super().__init__(reg, http, log, settings, (SHOW_IDS_FUNCTION,), "show-id")

    def read(self, release: dict) -> dict | None:
        """`{status, ids}` for one release (a record or a `{indexer, release_id}` reference), or None when no
        loaded add-on can be asked about it. `status` is "complete" (at least one usable id; any refused value is
        left out), "empty" (the add-on answered no id at all) or "failed" (the call raised, timed out, answered
        something that is not a dict, or answered only values the plugin refuses). `ids` is
        `normalize_show_ids`'s dict (all None unless complete). The one `indexers show_ids` line says which."""
        found = self.target(release)
        if found is None:
            return None
        ix, ref = found
        rid = ref.release_id
        query = ShowIdsQuery(release_id=rid, info_hash=str(release.get("info_hash") or ""),
                             title=str(release.get("title") or ""))
        read: dict = {}

        def summary(answer: dict) -> tuple[str, dict]:
            read["result"], fields = _show_ids_reading(ix.file, answer)
            read["ids"] = {k: fields["ids"].get(k) for k in SHOW_ID_KEYS}
            return read["result"], fields

        answer = self._call(ix, SHOW_IDS_FUNCTION, query, None, self.budget_s, {"release_id": rid}, dict, summary)
        empty = {k: None for k in SHOW_ID_KEYS}
        if answer is None or read.get("result") == "FAIL":
            return {"status": "failed", "ids": empty}
        if read.get("result") == "EMPTY":
            return {"status": "empty", "ids": empty}
        return {"status": "complete", "ids": read["ids"]}
