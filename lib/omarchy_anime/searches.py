"""The episode's last search, kept for the release picker.

One file per episode, `CACHE_DIR/searches/<anilist>-<episode>.json`, written by every resolve,
including one that refuses. It holds what the picker lists — the releases that passed in the
ranker's order and the rejected ones with a reason — and the raw release records by infohash, so
`play --release` plays the release as the index served it: an indexer's magnet may carry its trackers
only there, and rebuilding one from a hash loses them.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .core import CACHE_DIR, Log, StageError, cached_json, write_cache
from .score import guard_reading, pack_ranges, parse_title, sibling_name
from . import say as saymod

SEARCH_TTL = 30 * 60          # the picker reuses a search younger than this
SEARCH_DIR = CACHE_DIR / "searches"
_ROW_KEYS = ("id", "info_hash", "title", "total_size", "seeders", "num_files", "verified_langs", "has_lang",
             "rd_cached", "source")


def path(anilist_id, episode) -> Path:
    return SEARCH_DIR / f"{int(anilist_id)}-{int(episode)}.json"


def _hash(release: dict) -> str:
    return str(release.get("info_hash") or "").lower()


def row(release: dict, reason: str | None = None, detail: str | None = None,
        variants: list[str] | None = None, movie: bool = False) -> dict:
    """What the picker shows for one release. No score: it means nothing to a viewer.
    `show` is the other show the title names, whatever the row's reason: under the Mimi OVA
    refusal most Paper Desert packs are rejected for "nobody sharing", and one *passed* row is
    Paper Desert too."""
    parsed = release.get("parsed") or parse_title(release.get("title", ""))
    out = {k: release.get(k) for k in _ROW_KEYS}
    out["info_hash"] = _hash(release)
    ranges = pack_ranges(release.get("title", "")) if parsed.get("batch") else []
    out.update(group=parsed.get("group"), resolution=parsed.get("resolution"), pack=bool(parsed.get("batch")),
               pack_range=list(ranges[0]) if ranges else None,
               file=(release.get("file") or {}).get("name") if release.get("file") else None)
    # A movie's release without an episode marker names no show (`movie`, from `Search.movie`).
    name = sibling_name(release.get("title", ""), variants, movie) if variants else None
    out["show"] = name.title() if name else None
    # A `series` row says "another show" only when its title names one.
    out.update(reason=reason, why=saymod.rejected_why(reason, detail, named=bool(name)) if reason else None)
    return out


class Search:
    """The picker's view of one resolve. `pick` is the infohash `play --release` asked for: a pick
    reads the stored search and only ever marks its own row, so the list survives it."""

    def __init__(self, anilist_id, episode, log: Log, pick: str | None = None, keep: bool = True):
        self.anilist_id, self.episode, self.log = int(anilist_id), int(episode), log
        self.pick = (pick or "").lower() or None
        # False for a resolve whose list is not the episode's (the prefetch's one release, replayed): nothing is
        # written, so the release list never shows that list, a refusal or a failed row from it.
        self.keep = keep
        self.raw: dict[str, dict] = {}
        self.passed: list[dict] = []
        self.rejected: list[dict] = []
        self.searched = False
        self.variants: list[str] = []
        self.movie = False                     # the entry is a movie; saved with the search
        self.query: str | None = None          # the viewer's own words, when this list is theirs

    def add_raw(self, releases) -> None:
        self.searched = True
        for r in releases:
            if _hash(r):
                self.raw.setdefault(_hash(r), dict(r))

    def reject(self, release: dict, code: str, detail: str | None = None) -> None:
        h = _hash(release)
        if h in self.raw or release.get("magnet") or release.get("magnet_uri"):
            self.raw.setdefault(h, {k: v for k, v in release.items() if k != "parsed"})
        self.passed = [p for p in self.passed if p["info_hash"] != h]
        self.rejected = [p for p in self.rejected if p["info_hash"] != h]
        self.rejected.append(row(release, code, detail, self.variants, self.movie))

    def set_passed(self, ranked: list[dict]) -> None:
        gone = {r["info_hash"] for r in self.rejected}
        self.passed = [row(c, variants=self.variants, movie=self.movie) for c in ranked if _hash(c) not in gone]

    def mark_other_shows(self, ranked: list[dict], variants: list[str], movie: bool | None = None,
                         own=None) -> int:
        """The franchise refusal names the franchise as a whole; the picker names each release's show. A refused
        movie's own releases (no episode marker) are not another show. With the entry's titles (`own`), the show is
        the guard's reading (`guard_reading`), so a row is another show only by a name the guard counted, and is
        labelled by it (`S1 - Vol 1-9` was "another show: Vol", a word the guard sets aside). The count of
        rows marked."""
        movie = self.movie if movie is None else movie
        n = 0
        for cand in ranked:
            title = cand.get("title", "")
            name = guard_reading(title, variants, movie, own)[0] if own is not None else sibling_name(title, variants,
                                                                                                       movie)
            if name:
                self.reject(cand, "other_show", name)
                self.rejected[-1]["show"] = name.title()
                n += 1
        return n

    def save(self, refusal: dict | None = None) -> None:
        if self.pick or not self.searched or not self.keep:
            return
        data = {"anilist_id": self.anilist_id, "episode": self.episode, "searched_at": time.time(),
                "trace": self.log.trace, "movie": self.movie, "passed": self.passed, "rejected": self.rejected,
                "refusal": refusal, "failed": {}, "raw": self.raw, "query": self.query}
        _write(path(self.anilist_id, self.episode), data)
        self.log.info("releases.cache", "write", result="PASS", anilist=self.anilist_id, episode=self.episode,
                      passed=len(self.passed), rejected=len(self.rejected),
                      refusal=(refusal or {}).get("stage"))

    def refused(self, exc: StageError) -> None:
        """A resolve that stopped: the list is still worth showing, with why auto-play gave up. On a
        pick, the one row it tried is marked instead and nothing else changes."""
        if not self.keep:
            return
        if self.pick:
            mark_failed(self.anilist_id, self.episode, self.pick, str(exc), exc.stage, self.log)
            return
        self.save({"stage": exc.stage, "error": str(exc), "suggest": exc.suggest})


def _write(p: Path, data: dict) -> None:
    write_cache(p, data)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def load(anilist_id, episode, max_age: float | None = SEARCH_TTL) -> dict | None:
    """Age is `searched_at`, not the file's mtime: marking a failed pick rewrites the file, and that
    must not make an old search look fresh."""
    data = cached_json(path(anilist_id, episode), None)
    if not (isinstance(data, dict) and isinstance(data.get("raw"), dict)):
        return None
    if max_age is not None and time.time() - float(data.get("searched_at") or 0) > max_age:
        return None
    return data


def find_release(anilist_id, episode, info_hash: str) -> dict | None:
    """The raw release for a pick, from a stored search of any age: the pick came from it."""
    data = load(anilist_id, episode, None)
    return (data or {}).get("raw", {}).get(str(info_hash or "").lower())


def raw_releases(anilist_id, episode) -> list[dict]:
    """Every release the stored search saw, of any age: the donor pool for a pick."""
    return list(((load(anilist_id, episode, None) or {}).get("raw") or {}).values())


def mark_failed(anilist_id, episode, info_hash: str, error: str, stage: str, log: Log) -> None:
    data = load(anilist_id, episode, None)
    if data is None:
        return
    data.setdefault("failed", {})[str(info_hash).lower()] = {"stage": stage, "error": error, "at": time.time()}
    _write(path(anilist_id, episode), data)
    log.info("releases.cache", "failed", anilist=int(anilist_id), episode=int(episode), info_hash=info_hash,
             failed_stage=stage, error=error)


def public(data: dict, from_cache: bool) -> dict:
    """What `releases` prints: everything but the raw records (the magnets stay on disk)."""
    return {"anilist_id": data["anilist_id"], "episode": data["episode"], "searched_at": data["searched_at"],
            "age_s": round(time.time() - float(data["searched_at"])), "from_cache": from_cache,
            "releases": data.get("passed") or [], "rejected": data.get("rejected") or [],
            "refusal": data.get("refusal"), "failed": data.get("failed") or {}, "query": data.get("query")}
