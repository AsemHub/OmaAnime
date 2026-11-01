"""Side-loaded subtitles: find the preferred language in a sibling release of the same episode,
download that one track, cache it, and hand mpv a file.

A release's tracks and a track's text come from the indexer add-on that returned the
release (`indexers.SubtitleTracks`); with no add-on offering them there is no donor and no retiming.

Why this exists, measured against the user's own Continue Watching list (15 entries,
next episode of each): the preferred language exists in **some** indexed release for 14/15, but in
the **most-seeded** release for only 4/15. The scorer already prefers a release that embeds the
language when it can see one; this covers the cases where that release is unstreamable (dead
torrent, uncached on every provider) or is worse in every other respect.

Everything here is best-effort and never raises into the play path: each step has its own
budget, a failure logs a line with a `suggest` and falls through to exactly today's behaviour.

Verified live on 2026-09-16 (the four tracks the tests are cut from):

    400144 ara ASS  7,458,894 bytes, 9,240 cues   1258 ms
    400143 ara SRT     23,077 bytes,   250 cues    328 ms
    400143 eng SRT     20,166 bytes,   308 cues    300 ms
    400145 eng SRT     25,818 bytes,   389 cues    311 ms
"""
from __future__ import annotations

import bisect
import codecs
import os
import re
import time
import traceback
from pathlib import Path

from . import core
from .core import (CACHE_DIR, Http, Log, LANGUAGE_TABLE, as_int, cached_json, fold_language, same_language,
                   language_name as core_language_name,
                   write_cache, write_bytes_atomic)
from .indexers import MAX_PLAIN_BYTES, SUBTITLE_FORMATS, SubtitleTracks, release_key, tracks_key
from .score import parse_title, title_matches_episode

SUBS_DIR = CACHE_DIR / "subs"
RAW_DIR = SUBS_DIR / "raw"
STAMP_NAME = ".pruned"


EXT_FOR_FORMAT = {"ASS": ".ass", "SSA": ".ass", "SRT": ".srt"}

DONOR_WORKERS = 4
DONOR_BUDGET_S = 1.5

PRUNE_MAX_AGE_DAYS = 30.0
PRUNE_MAX_BYTES = 16 * 2**20
PRUNE_EVERY_S = 86400

TIER_SAME_GROUP_AND_SOURCE = 0
TIER_SAME_SOURCE = 1
TIER_SHARED_ANCHOR = 2
TIER_ANY = 3
TIER_NAMES = {0: "same group and source", 1: "same source", 2: "shared anchor language", 3: "unrelated"}

# `parse_title` reads a source token out of the title, and its alternation includes the generic
# `WEB`, `BD`, `BluRay`, `DVD`. Two unrelated rips can therefore both parse as "WEB" — measured
# 2026-09-16: `[Idler] … (WEB 1080p EAC3)` and `[Zafir] … Shahid.WEB-DL` both do. Treating that as
# "same streaming source" would put them in the measured zero-offset class and skip the estimate
# while claiming the timing was verified. A generic token means "source unknown" here.
GENERIC_SOURCES = {"WEB", "BD", "BLURAY", "DVD", ""}


def streaming_source(parsed: dict) -> str:
    src = str(parsed.get("source") or "").upper()
    return "" if src in GENERIC_SOURCES else src

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def language_aliases(lang: str) -> set[str]:
    """The same alias set the scorer uses, so a side-load works for `tur`, `per`, … not only `ara`. A tag is tested
    against it through `in_aliases` (a region or script folds to its language)."""
    return {a.lower() for a in LANGUAGE_TABLE.get(lang, ((lang,), ()))[0]} | {str(lang).lower()}


def in_aliases(tag, aliases: set[str]) -> bool:
    """A track tag is one of `language_aliases`' languages: the tag itself, or its table code (`ar-SA` -> `ara`)."""
    return str(tag or "").lower() in aliases or (fold_language(tag) or "") in aliases


def normalize_lang(tag: str) -> str:
    """One ISO 639-2 code for a language tag from either side of the fence.

    The index writes 639-2 (`eng`, `por`, `spa`); **mpv's `track-list` writes BCP-47** — measured on a
    real Disney+ rip 2026-09-16: `en`, `pt-BR`, `es-419`, `de`, `fr`, `it`. Comparing the two
    verbatim finds no shared anchor language at all, which silently demotes every donor to the
    unrelated tier and loads it unshifted. Region subtags are dropped and aliases folded (`core.fold_language`); a
    language the table does not hold keeps its first subtag (`bg`, which `CODEPAGES` folds itself).
    """
    return fold_language(tag) or str(tag or "").lower().replace("_", "-").split("-")[0]


# ----------------------------------------------------------------------------- donor search

def eligible_tracks(cand: dict, aliases: set[str], episode: int, alt=None,
                    episode_unstated: bool = False) -> list[dict]:
    """Tracks of one release that could stand in for the missing language.

    The `?eid=` key is already episode-exact, so `title_matches_episode` here is the second,
    independent guard against a mis-indexed record (the retiming's match ratio is the third).
    `episode_unstated` is a movie's or a one-episode entry's search, whose releases need not state the
    episode (the release filter does not ask it of them): their names are read as before, any number.
    """
    tracks = cand.get("tracks")
    if not tracks:
        return []
    parsed = cand.get("parsed") or parse_title(str(cand.get("title") or ""))
    if parsed.get("batch"):
        # A season pack's attachment list is every episode's tracks with nothing to tell them apart,
        # so it can never be a donor for one episode.
        return []
    if not title_matches_episode(str(cand.get("title") or ""), episode, alt, any_number=episode_unstated):
        return []
    out = []
    for t in tracks:
        if not in_aliases(t.get("lang"), aliases):
            continue
        if (t.get("format") or "").upper() not in SUBTITLE_FORMATS:
            continue
        if not t.get("url"):
            continue
        out.append(t)
    return out


def donor_tier(chosen_parsed: dict, cand_parsed: dict, chosen: dict, cand: dict) -> tuple[int, str | None]:
    """Which tier this donor sits in, and the anchor language it shares with the target (if any).

    1. same release group **and** same streaming source — the same group's twin of the same rip:
       identical timebase, identical translation;
    2. same streaming source — the measured zero-offset class (Kumo-raws `ara` lands on the
       SubsKindly release's own `eng` grid 213/213 exact, max |delta| 0.000 s, re-measured today);
    3. shares an anchor language with the target, so the offset can be estimated;
    4. anything else — loaded at offset 0 with the timing flagged unverified.

    Tiers 1 and 2 do not depend on an anchor and are decided before it is looked for: the estimate
    short-circuits a same-source pair with the measured zero offset, so an absent anchor record must
    not demote a donor that needs no estimate.
    """
    anchor = _shared_anchor(chosen, cand)
    cs, ds = streaming_source(chosen_parsed), streaming_source(cand_parsed)
    cg = str(chosen_parsed.get("group_key") or "")
    dg = str(cand_parsed.get("group_key") or "")
    if cs and cs == ds and cg and cg == dg:
        return TIER_SAME_GROUP_AND_SOURCE, anchor
    if cs and cs == ds:
        return TIER_SAME_SOURCE, anchor
    if anchor:
        return TIER_SHARED_ANCHOR, anchor
    return TIER_ANY, None


ANCHOR_PREFERENCE = ("eng", "spa", "por", "fre", "ger", "ita")


def _anchor_langs(release: dict) -> set[str]:
    """Languages of this release for which a *downloadable* anchor track exists — the same predicate
    `_anchor_track()` applies, so the tier cannot promise an anchor the estimator will not find."""
    return {normalize_lang(t.get("lang")) for t in (release.get("tracks") or [])
            if not t.get("forced") and (t.get("format") or "").upper() in SUBTITLE_FORMATS and t.get("url")}


def _shared_anchor(chosen: dict, cand: dict) -> str | None:
    """The first language downloadable from **both** releases' attachment records, preferring `eng`
    (109 of 114 sampled releases carry one; the five that did not were French-only ADN/DSNP rips).

    This used to compare mpv's *language list* for the target against the donor's
    *records*, so a target whose languages were known but whose records were not got tier 2 and a
    badge saying "shared anchor language" over an offset of 0 — the estimator needs a record, not a
    name, and the two halves have to apply the same predicate.
    """
    shared = _anchor_langs(chosen) & _anchor_langs(cand)
    for pref in ANCHOR_PREFERENCE:
        if pref in shared:
            return pref
    return sorted(shared)[0] if shared else None


# A donor as `meta.json` keeps it: the release is its indexer's name plus that indexer's release id, the
# indexer its tracks were read from (`tracks_key`, since `track_id` is that indexer's).
DONOR_KEYS = ("indexer", "release_id", "info_hash", "title", "group", "source", "seeders")


def _donor_record(cand: dict, track: dict, tier: int, anchor: str | None) -> dict:
    parsed = cand.get("parsed") or parse_title(str(cand.get("title") or ""))
    return {
        **tracks_key(cand).as_dict(),
        "info_hash": cand.get("info_hash"), "title": cand.get("title"),
        "group": parsed.get("group"), "source": parsed.get("source"), "seeders": cand.get("seeders") or 0,
        "tier": tier, "tier_name": TIER_NAMES.get(tier, "?"), "anchor_lang": anchor,
        "track_id": track.get("id"), "lang": track.get("lang"), "format": track.get("format"),
        "forced": bool(track.get("forced")), "default": bool(track.get("default")),
        "size": track.get("size"), "url": track.get("url"),
        # The donor release's whole track list, so the anchor language can be fetched without
        # another index lookup. Private: the CLI and now-playing pick explicit keys.
        "_tracks": cand.get("tracks") or [],
    }


def _order_key(d: dict) -> tuple:
    """Within a tier: an official-source track before a fansub, `default` before not, then more
    seeders, then the smaller file (a 7.5 MB karaoke ASS is a worse donor than a 26 KB one)."""
    return (d["tier"], 0 if d.get("source") else 1, 0 if d["default"] else 1,
            -(d.get("seeders") or 0), d.get("size") or 0)


def order_donors(donors: list[dict]) -> list[dict]:
    """Non-forced donors first, in tier order; forced (signs-only) ones only when nothing else is
    left, because a signs-only track is not a subtitle track."""
    plain = sorted([d for d in donors if not d["forced"]], key=_order_key)
    forced = sorted([d for d in donors if d["forced"]], key=_order_key)
    return plain + forced


def find_donor(http: Http, log: Log, releases: list[dict], chosen: dict, settings: dict, *,
               episode: int, alt=None, budget_s: float = DONOR_BUDGET_S,
               workers: int = DONOR_WORKERS, track_source: SubtitleTracks | None = None,
               episode_unstated: bool = False) -> dict:
    """Every eligible donor for `chosen`, best first. Index only — nothing is downloaded here.

    Sources, in order: releases already verified during resolve (free — their `tracks` came with the
    ranking), then the remaining releases whose indexer offers subtitle tracks (`track_source`),
    swept with a small thread pool (measured: 32 records in 1.40 s with
    four workers, no queueing). A release whose indexer does not offer them is skipped.
    `episode_unstated` (a movie, a one-episode entry) reads a donor's name for the episode as before
    (`eligible_tracks`).
    """
    t0 = time.monotonic()
    lang = settings["subtitleLanguage"]
    aliases = language_aliases(lang)
    chosen_parsed = chosen.get("parsed") or parse_title(str(chosen.get("title") or ""))
    # The target's own attachment *records* decide tier 3 (a shared anchor language) and the
    # retiming: `estimate_from_anchors` downloads the target's anchor track by url, and only the
    # index record carries that url. Worth one lookup (a median 0.92 s measured), permanently cached.
    #
    # This guard used to key on `verified_langs`, which `prepare()` fills from mpv's
    # `track-list` immediately before calling here — so on every real play the branch was dead,
    # `chosen["tracks"]` stayed empty and the estimate bailed with "no eng attachment on both
    # sides", loading the donor unshifted. mpv is the truth about *what the file contains*; the
    # index record is the only source for *where the anchor can be downloaded*. Both are kept.
    if not chosen.get("tracks") and track_source is not None and track_source.can_read(chosen):
        rec = track_source.read(chosen)
        chosen["tracks"], chosen["tracks_from"] = rec["tracks"], rec["read_from"]
        if chosen.get("verified_langs") is None:
            chosen["verified_langs"] = rec["languages"]
    target_langs = {(l or "").lower() for l in (chosen.get("verified_langs") or [])}

    chosen_key = release_key(chosen)

    def collect(pool: list[dict]) -> list[dict]:
        out = []
        for cand in pool:
            if release_key(cand) == chosen_key:
                continue
            for track in eligible_tracks(cand, aliases, episode, alt, episode_unstated):
                tier, anchor = donor_tier(chosen_parsed, cand.get("parsed") or parse_title(str(cand.get("title") or "")),
                                          chosen, cand)
                out.append(_donor_record(cand, track, tier, anchor))
        return out

    known = [c for c in releases if c.get("tracks")]
    donors = collect(known)
    examined, swept, from_cache = len(known), 0, True
    if not donors:
        from_cache = False
        todo = [c for c in releases if release_key(c) != chosen_key and not c.get("tracks")
                and track_source is not None and track_source.can_read(c)]
        swept = _sweep(log, track_source, todo, budget_s=budget_s, workers=workers)
        examined += len(todo)
        donors = collect(known + todo)

    donors = order_donors(donors)
    ms = (time.monotonic() - t0) * 1000
    log.info("subs.donor", "scan", result="PASS" if donors else "EMPTY", ms=ms, lang=lang,
             examined=examined, swept=swept, eligible=len(donors), workers=workers,
             from_cache=from_cache,
             suggest="" if donors else (f"no donor: {examined} releases read, none carries {lang} — the source "
                                        f"platform likely has no {lang} for this title"))
    return {"lang": lang, "donors": donors, "examined": examined, "swept": swept, "ms": round(ms, 1),
            "from_cache": from_cache, "target_langs": sorted(target_langs)}


def _sweep(log: Log, track_source: SubtitleTracks, todo: list[dict], *, budget_s: float, workers: int) -> int:
    """Fill in `tracks` for releases the resolve never verified. Bounded by `budget_s`, each add-on
    call included (a hung one is abandoned at the deadline, not held for `indexerTimeoutSeconds`);
    whatever the budget cuts off simply stays unverified for this play."""
    if not todo:
        return 0
    import concurrent.futures
    deadline = time.monotonic() + max(0.1, budget_s)
    done = 0

    def read(cand: dict) -> dict:
        return track_source.read(cand, budget_s=deadline - time.monotonic(), deadline_of="the donor sweep's budget")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(read, c): c for c in todo}
        try:
            for fut in concurrent.futures.as_completed(futures, timeout=max(0.1, deadline - time.monotonic())):
                cand = futures[fut]
                try:
                    rec = fut.result()
                except Exception as exc:                      # noqa: BLE001 - one bad id must not stop the sweep
                    log.warn("subs.donor", "lookup", result="FAIL", id=cand.get("id"), error=str(exc))
                    continue
                cand["tracks"], cand["tracks_from"] = rec["tracks"], rec["read_from"]
                if cand.get("verified_langs") is None:
                    cand["verified_langs"] = rec["languages"]
                done += 1
        except concurrent.futures.TimeoutError:
            for fut in futures:
                fut.cancel()
            log.warn("subs.donor", "budget", result="FAIL", budget_s=budget_s, done=done, todo=len(todo),
                     suggest="the donor sweep ran out of budget; the rest stays unverified for this play")
    return done


# ----------------------------------------------------------------------------- download

# The codepages a legacy (not UTF-8) subtitle file of each language is tried in, strictly and in
# order, after its BOM and strict UTF-8. The table is subliminal's `find_potential_encodings`
# (src/subliminal/subtitle.py), which Bazarr's `guess_encoding` repeats; keyed by ISO 639-2 code, both forms.
# Left out: `ascii` (Arabic's list) and the chardet/uchardet step (no dependency here). UTF-16 without a BOM
# (`utf-16`/`utf-16le` in Arabic's list, the trailing `utf-16` in Chinese's) is not handled: subliminal reaches
# it because `isprintable()` rejects the NULs a UTF-8 or cp1256 read of it gives; here it reads as garbage, as
# before. It is rare: Notepad's "Unicode" writes a BOM, and a file with a BOM is read by `_BOMS`.
_WESTERN = ("cp1252", "iso-8859-15", "iso-8859-9", "iso-8859-4", "iso-8859-1")
_CENTRAL = ("cp1250", "iso-8859-2")
_CYRILLIC = ("cp1251", "iso-8859-5")
CODEPAGES: dict[str, tuple[str, ...]] = {
    **{c: ("cp936", "gb2312", "gbk", "hz", "iso2022_jp_2", "cp950", "big5hkscs", "big5", "gb18030")
       for c in ("zho", "chi")},
    "jpn": ("shift_jis", "cp932", "euc_jp", "iso2022_jp", "iso2022_jp_1", "iso2022_jp_2", "iso2022_jp_2004",
            "iso2022_jp_3", "iso2022_jp_ext"),
    "tha": ("tis-620", "cp874"),
    "ara": ("cp1256", "iso-8859-6"), "fas": ("cp1256", "iso-8859-6"), "per": ("cp1256", "iso-8859-6"),
    "heb": ("cp1255", "iso-8859-8"),
    "tur": ("cp1254", "iso-8859-9", "iso-8859-3"),
    **{c: ("cp1253", "cp737", "iso-8859-7", "cp875", "cp869", "iso2022_jp_2", "mac_greek")
       for c in ("grc", "gre", "ell")},
    **{c: _CENTRAL for c in ("pol", "cze", "ces", "slk", "slo", "hun", "bos", "hbs", "hrv", "rsb", "ron", "rum")},
    "slv": _CENTRAL + ("iso-8859-4",),
    **{c: _CENTRAL + ("cp1252", "iso-8859-15", "iso-8859-1", "iso-8859-9") for c in ("sqi", "alb")},
    **{c: _CYRILLIC for c in ("bul", "mkd", "mac", "rus", "ukr")},
    "srp": ("cp1250", "cp1251", "iso-8859-2", "iso-8859-5"),        # script unknown: their no-script list
    # Two of the new languages subliminal's table leaves to the Western list, whose legacy files are in
    # their own Windows codepage (Korean 949, a superset of EUC-KR; Vietnamese 1258). Every other new language is in
    # the table above (chi, jpn, tha, heb, gre, cze, slo, hun, bos, hrv, rum, ukr) or is written in the Western
    # codepage (ind, may, dan, fin, dut, swe, nor, fil, cat, glg, baq) or has none (hin, sin: no Windows codepage).
    "kor": ("cp949", "euc_kr"),
    "vie": ("cp1258",),
}
# The two-letter tags `normalize_lang` leaves as they are (it folds only `LANGUAGE_TABLE`'s languages).
_CODEPAGE_ALIASES = {"zh": "zho", "ja": "jpn", "th": "tha", "he": "heb", "iw": "heb", "el": "gre", "cs": "cze",
                     "sk": "slo", "sl": "slv", "hu": "hun", "bs": "bos", "hr": "hrv", "ro": "rum", "sq": "alb",
                     "bg": "bul", "mk": "mac", "uk": "ukr", "sr": "srp"}
_BOMS = ((codecs.BOM_UTF8, "utf-8-sig", "utf-8"),
         # UTF-32's little-endian BOM starts with UTF-16's, so UTF-32 is asked first.
         (codecs.BOM_UTF32_BE, "utf-32", "utf-32-be"), (codecs.BOM_UTF32_LE, "utf-32", "utf-32-le"),
         (codecs.BOM_UTF16_BE, "utf-16", "utf-16-be"), (codecs.BOM_UTF16_LE, "utf-16", "utf-16-le"))
# A C1 control in decoded text means a codepage that maps 0x80-0x9F to them (the ISO-8859 ones) read a file
# written in another; so a decode that makes one fails like any other. Not `str.isprintable()`, which
# subliminal asks: cp1256 maps 0x9D/0x9E/0xFD/0xFE to ZWNJ, ZWJ, LRM and RLM (format characters, not
# printable), which Persian and Arabic subtitles use.
_C1 = re.compile("[\x80-\x9f]")
# What every subtitle file the plugin writes (a retimed donor, an external file, its shifted copy) is in.
WRITTEN_ENCODING = "utf-8"


def codepages_for(lang) -> tuple[str, str, tuple[str, ...]]:
    """`(code, why, codepages)` for a subtitle file in `lang`: the language's legacy codepages, else the
    Western list (subliminal's own default for every other language). `code` is `und` when `lang` is empty."""
    code = normalize_lang(lang) if lang else "und"
    code = _CODEPAGE_ALIASES.get(code, code)
    return code, f"lang:{code}", CODEPAGES.get(code, _WESTERN)


def misread(encoding, lang) -> bool:
    """True for a file read in a single-byte codepage that is not one of `lang`'s: every non-UTF-8
    track an older build read became cp1256, Arabic's, whatever its language."""
    enc = str(encoding or "").lower()
    return bool(enc) and not enc.startswith("utf-") and enc not in codepages_for(lang)[2]


def decode_text(blob: bytes, lang: str | None = None, log: Log | None = None, **who) -> tuple[str, str]:
    """A subtitle file's text and the encoding it was read in.

    In order: the BOM's encoding; strict UTF-8; each of `lang`'s codepages strictly (`codepages_for`). A
    language's codepages are tried only when it is given: a caller that does not know the file's language
    gets the Western list. Last, the first of those codepages with `errors="replace"`: the only decode that
    cannot fail, logged at warn with the count of replaced bytes. A UTF-8 BOM reads as `utf-8`, BOM stripped.

    With `log`, one `subs.decode` line per call: the encoding, why (`bom`, `utf8`, `lang:<code>`,
    `fallback`), the codepages that failed first, and how many characters were replaced. `who` names the file.
    Every file the plugin writes from this text is UTF-8: mpv takes "data that looks like UTF-8" as UTF-8
    before its own `--sub-codepage` guess."""
    code, why, codepages = codepages_for(lang)
    text, encoding, reason, tried = None, "", "", []
    for bom, codec, name in _BOMS:
        if blob.startswith(bom):
            try:
                text, encoding, reason = blob.decode(codec), name, "bom"
            except UnicodeDecodeError:
                tried.append(name)
            break
    if text is None:
        try:
            text, encoding, reason = blob.decode("utf-8"), "utf-8", "utf8"
        except UnicodeDecodeError:
            tried.append("utf-8")
    if text is None:
        for codec in codepages:
            try:
                got = blob.decode(codec)
            except (UnicodeDecodeError, LookupError):
                tried.append(codec)
                continue
            if _C1.search(got):
                tried.append(codec)
                continue
            text, encoding, reason = got, codec, why
            break
    replaced = 0
    if text is None:
        text, encoding, reason = blob.decode(codepages[0], errors="replace"), codepages[0], "fallback"
        replaced = text.count("�")
    if log is not None:
        level = "warn" if reason == "fallback" else ("info" if reason.startswith("lang:") else "debug")
        log.emit(level, "subs.decode", "decode", result="WARN" if reason == "fallback" else "PASS",
                 encoding=encoding, why=reason, lang=lang or None, code=code, tried=tried or None,
                 replaced=replaced, bytes=len(blob), **who,
                 suggest=(f"no codepage of {code} reads this file strictly; {replaced} byte(s) shown as U+FFFD"
                          if reason == "fallback" else ""))
    return text, encoding


def raw_path(indexer, track_id, fmt: str) -> Path:
    """`raw/<indexer>/<track id>.<ext>`: two indexers may number their tracks alike, and
    neither name nor id comes from this plugin, so both are made path-safe."""
    folder = _SAFE.sub("_", str(indexer or "unknown")).strip("._") or "unknown"
    name = _SAFE.sub("_", str(track_id)).strip("._") or "x"
    return RAW_DIR / folder / f"{name}{EXT_FOR_FORMAT.get((fmt or '').upper(), '.srt')}"


def fetch_track(log: Log, track_source: SubtitleTracks | None, ref: dict) -> dict | None:
    """One subtitle track's text, through the add-on that listed it (`track_source`), kept in `subs/raw/<indexer>/<track id>.<ext>`. Never raises.

    `ref` names the release (`indexer`, `release_id`) and the track (`track_id`, `format`, `url`, `lang`),
    as a donor record does. Returns `{path, bytes_plain, encoding, format, cached, text}` or None with a
    logged reason. The extension comes from the record's `format`, never from the URL or any header.
    """
    fmt = (ref.get("format") or "").upper()
    url = ref.get("url") or ""
    tid = ref.get("track_id")
    who = {"indexer": ref.get("indexer"), "release_id": ref.get("release_id"), "track_id": tid}
    if fmt not in EXT_FOR_FORMAT or not url or tid is None:
        log.warn("subs.fetch", "refuse", result="FAIL", **who, format=fmt, has_url=bool(url),
                 suggest="a subtitle track needs an ASS/SSA/SRT format, an id and a URL")
        return None
    path = raw_path(ref.get("indexer"), tid, fmt)
    if path.exists() and path.stat().st_size > 0:
        blob = path.read_bytes()
        text, encoding = decode_text(blob, ref.get("lang"), log, **who, path=str(path))
        log.debug("subs.fetch", "cache", result="HIT", **who, path=str(path), bytes=len(blob))
        return {"path": str(path), "bytes_plain": len(blob), "encoding": encoding,
                "format": fmt, "cached": True, "text": text}
    if track_source is None or not track_source.can_read(ref):
        log.warn("subs.fetch", "no-indexer", result="FAIL", **who,
                 suggest="no loaded indexer add-on offers subtitle tracks for this release; nothing to fetch")
        return None
    t0 = time.monotonic()
    body = track_source.text(ref, {"id": tid, "lang": ref.get("lang"), "format": fmt, "url": url,
                                 "name": ref.get("name"), "forced": ref.get("forced")})
    ms = (time.monotonic() - t0) * 1000
    if body is None:
        log.warn("subs.fetch", "error", result="FAIL", **who, ms=ms,
                 suggest="the indexer add-on could not fetch the track (its indexers line above says why); "
                         "playing without it")
        return None
    plain = body.encode("utf-8") if isinstance(body, str) else bytes(body)
    if len(plain) > MAX_PLAIN_BYTES or not plain:
        log.warn("subs.fetch", "cap", result="FAIL", **who, ms=ms, bytes=len(plain),
                 suggest=f"the track is empty or larger than {MAX_PLAIN_BYTES} bytes; skipped")
        return None
    if isinstance(body, str):
        # The add-on decoded the file itself; its text is kept as UTF-8 and nothing is guessed here.
        text, encoding = body, "utf-8"
        log.debug("subs.decode", "decode", result="PASS", encoding=encoding, why="text", lang=ref.get("lang"),
                  replaced=body.count("\ufffd"), bytes=len(plain), **who)
    else:
        text, encoding = decode_text(plain, ref.get("lang"), log, **who)
    try:
        write_bytes_atomic(path, plain)
    except OSError as exc:
        log.warn("subs.fetch", "write", result="FAIL", **who, path=str(path), error=str(exc))
        return None
    log.info("subs.fetch", "done", result="PASS", ms=ms, **who, bytes_plain=len(plain), format=fmt,
             encoding=encoding)
    return {"path": str(path), "bytes_plain": len(plain), "encoding": encoding,
            "format": fmt, "cached": False, "text": text}


# ----------------------------------------------------------------------------- cue parsing and retiming

# Subtitle text is third-party: an hour starts at a non-digit, so a run of digits is tried once rather
# than from each of its digits, and nothing crosses a line. An hour longer than MAX_HOUR_DIGITS drops its cue
# (a 400-digit one overflows a float); recorded tracks write 1 or 2 digits.
_SRT_TIME = re.compile(r"(?<!\d)(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
                       r"[^\S\r\n]*-->[^\S\r\n]*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")
_ASS_DIALOGUE = re.compile(r"^(Dialogue:[^,\r\n]*,)(\d+:\d{2}:\d{2}\.\d{2}),(\d+:\d{2}:\d{2}\.\d{2}),", re.M)
_ASS_TIME = re.compile(r"^(\d+):(\d{2}):(\d{2})\.(\d{2})$")
MAX_HOUR_DIGITS = 3

MATCH_TOLERANCE_S = 0.12        # a cue "lands on" a target cue within 120 ms
BUCKET_S = 0.01                 # the histogram's resolution
SMOOTH_BUCKETS = 12             # +-120 ms of smoothing around each candidate offset
MIN_MATCH_RATIO = 0.30
DRIFT_BUCKET_S = 300.0          # 5-minute buckets for the drift guard
DRIFT_MIN_CUES = 5              # a bucket with fewer matched cues says nothing
DRIFT_MIN_BUCKETS = 3
MAX_DRIFT_SPAN_S = 0.5


def _srt_seconds(h, m, s, frac) -> float | None:
    if len(h) > MAX_HOUR_DIGITS:
        return None
    return int(h) * 3600 + int(m) * 60 + int(s) + int(frac) / (10 ** len(str(frac)))


def parse_cues(text: str, fmt: str, log: Log | None = None, source: str = "") -> list[tuple[float, float]]:
    """(start, end) of every cue, in seconds, sorted by start. ASS and SRT.

    A cue whose hour runs past MAX_HOUR_DIGITS is dropped; with `log`, one `subs.parse drop` line names how many
    (`source` says which file)."""
    out: list[tuple[float, float]] = []
    dropped, longest = 0, 0
    if (fmt or "").upper() in ("ASS", "SSA"):
        for m in _ASS_DIALOGUE.finditer(text):
            a, b = _ass_seconds(m.group(2)), _ass_seconds(m.group(3))
            if a is not None and b is not None:
                out.append((a, b))
            else:
                dropped += 1
                longest = max(longest, len(m.group(2).partition(":")[0]), len(m.group(3).partition(":")[0]))
    else:
        for m in _SRT_TIME.finditer(text):
            g = m.groups()
            a, b = _srt_seconds(g[0], g[1], g[2], g[3]), _srt_seconds(g[4], g[5], g[6], g[7])
            if a is not None and b is not None:
                out.append((a, b))
            else:
                dropped += 1
                longest = max(longest, len(g[0]), len(g[4]))
    if dropped and log is not None:
        log.warn("subs.parse", "drop", result="WARN", source=source, format=(fmt or "").upper(), dropped=dropped,
                 cues=len(out), hour_digits=longest, limit=MAX_HOUR_DIGITS,
                 suggest=f"{dropped} cue(s) with an hour of {longest} digits (no real file has more than "
                         f"{MAX_HOUR_DIGITS}) were left out; the other cues are used")
    out.sort()
    return out


def _ass_seconds(value: str) -> float | None:
    m = _ASS_TIME.match(value.strip())
    if not m or len(m.group(1)) > MAX_HOUR_DIGITS:
        return None
    h, mi, s, cs = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(cs) / 100


def _fmt_ass(seconds: float) -> str:
    seconds = max(0.0, seconds)
    cs = int(round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _fmt_srt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def shift_text(text: str, fmt: str, offset: float) -> str:
    """Rewrite every timestamp by `offset`, clamped at 00:00:00.

    The offset is baked into the file rather than applied with mpv's `sub-delay` because `sub-delay`
    is a **global player property**: it would also skew the embedded English track if the user
    switched back to it, it survives the next `loadfile`, and nothing in the service resets it.
    Cost measured at about 1 ms for a 389-cue file.
    """
    if not offset:
        return text
    if (fmt or "").upper() in ("ASS", "SSA"):
        def sub(m):
            a, b = _ass_seconds(m.group(2)), _ass_seconds(m.group(3))
            if a is None or b is None:
                return m.group(0)
            return f"{m.group(1)}{_fmt_ass(a + offset)},{_fmt_ass(b + offset)},"
        return _ASS_DIALOGUE.sub(sub, text)

    def sub_srt(m):
        g = m.groups()
        a, b = _srt_seconds(g[0], g[1], g[2], g[3]), _srt_seconds(g[4], g[5], g[6], g[7])
        if a is None or b is None:
            return m.group(0)
        return f"{_fmt_srt(a + offset)} --> {_fmt_srt(b + offset)}"
    return _SRT_TIME.sub(sub_srt, text)


def _match_ratio(donor: list[float], target: list[float], offset: float,
                 tolerance: float = MATCH_TOLERANCE_S) -> float:
    if not donor or not target:
        return 0.0
    hits = 0
    for c in donor:
        x = c + offset
        i = bisect.bisect_left(target, x)
        near = [abs(target[j] - x) for j in (i - 1, i) if 0 <= j < len(target)]
        if near and min(near) <= tolerance:
            hits += 1
    return hits / len(donor)


# The histogram visits every (donor, target) cue pair within the window: n×m when two hostile files put every cue
# inside it. The largest recorded comparison is 182,701 pairs (a 1,747-cue movie against itself, 180 s);
# past ALIGN_MAX_PAIRS the donor is thinned to every k-th cue, so the estimate degrades instead of running for minutes.
ALIGN_MAX_PAIRS = 2_000_000


def _thinned(donor: list[float], target: list[float], window: float, max_pairs: int) -> tuple[list[float], int, int]:
    """`(donor cues to compare, k, pairs)`: every cue when its pairs fit `max_pairs`, else every k-th, k doubled
    until they fit (or one cue is left)."""
    counts = [bisect.bisect_right(target, c + window) - bisect.bisect_left(target, c - window) for c in donor]
    pairs = sum(counts)
    if pairs <= max_pairs:
        return donor, 1, pairs
    k = -(-pairs // max(1, max_pairs))
    while k < len(donor) and sum(counts[::k]) > max_pairs:
        k *= 2
    return donor[::k], k, pairs


def estimate_offset(donor: list[float], target: list[float], max_offset: float = 60.0, *,
                    max_pairs: int = ALIGN_MAX_PAIRS, log: Log | None = None) -> tuple[float, float]:
    """One constant offset aligning the donor's cue starts onto the target's, plus the match ratio.

    A histogram of pairwise deltas, not a brute-force scan. Measured on the real cross-source pair
    (`[Zafir]` Shahid -> DSNP QORVX, THISTLE ep 44): **+22.810 s, 156/308 = 50.6 %, 30-44 ms**. The
    brute-force +-60 s / 10 ms scan gives the same +22.810 s in 1,797 ms —
    40x slower for an identical answer.

    A 50 % ratio is normal, not suspicious: two translators split lines differently and no aligner
    fixes that. The separation that matters is against a *wrong* episode, measured at 10.8 % and
    7.8 % on the same data, which is why the acceptance threshold sits at 30 %.

    Past `max_pairs` cue pairs the histogram reads every k-th donor cue (`subs.offset sample` with `log`); the
    match ratio still reads them all.
    """
    if not donor or not target or max_offset <= 0:
        return 0.0, 0.0
    sample, every, pairs = _thinned(donor, target, max_offset, max_pairs)
    if every > 1 and log is not None:
        log.warn("subs.offset", "sample", result="WARN", pairs=pairs, limit=max_pairs, every=every,
                 donor_cues=len(donor), target_cues=len(target), window=max_offset,
                 suggest=f"{pairs} cue pairs is past the {max_pairs} limit; every {every}th donor cue was compared")
    hist: dict[int, int] = {}
    for c in sample:
        lo = bisect.bisect_left(target, c - max_offset)
        hi = bisect.bisect_right(target, c + max_offset)
        for t in target[lo:hi]:
            k = int(round((t - c) / BUCKET_S))
            hist[k] = hist.get(k, 0) + 1
    if not hist:
        return 0.0, 0.0
    best_k = max(hist, key=lambda k: (sum(hist.get(k + d, 0) for d in range(-SMOOTH_BUCKETS, SMOOTH_BUCKETS + 1)), -abs(k)))
    offset = best_k * BUCKET_S
    return round(offset, 3), round(_match_ratio(donor, target, offset), 4)


def drift_span(donor: list[float], target: list[float], offset: float) -> tuple[float | None, dict]:
    """Median residual per 5-minute bucket after applying `offset`, and the span across buckets.

    A span wider than half a second is a framerate stretch or an ad-break split, which one constant
    cannot fix. Measured on the accepted real pair: +0.007, -0.009, +0.005, -0.001, -0.094 —
    a span of 0.101 s. Buckets with fewer than five matched cues are ignored (they say nothing); if
    fewer than three buckets survive, the span is `None` and the ratio decides alone.
    """
    buckets: dict[int, list[float]] = {}
    for c in donor:
        x = c + offset
        i = bisect.bisect_left(target, x)
        near = [(abs(target[j] - x), target[j] - x) for j in (i - 1, i) if 0 <= j < len(target)]
        if not near:
            continue
        dist, signed = min(near)
        if dist <= 0.25:
            buckets.setdefault(int(c // DRIFT_BUCKET_S), []).append(signed)
    medians = {k: round(_median(v), 3) for k, v in sorted(buckets.items()) if len(v) >= DRIFT_MIN_CUES}
    if len(medians) < DRIFT_MIN_BUCKETS:
        return None, medians
    return round(max(medians.values()) - min(medians.values()), 3), medians


# How far around the global offset each 5-minute bucket looks for its own offset, and how
# strongly the cues must land there for the bucket to count as moved.
LOCAL_WINDOW_S = 10.0
MOVED_MATCH_RATIO = 0.5


def _landed(cues: list[float], target: list[float]) -> float:
    n = 0
    for x in cues:
        i = bisect.bisect_left(target, x)
        if any(0 <= j < len(target) and abs(target[j] - x) <= MATCH_TOLERANCE_S for j in (i - 1, i)):
            n += 1
    return n / len(cues) if cues else 0.0


def local_offsets(donor: list[float], target: list[float], offset: float, *,
                  max_pairs: int = ALIGN_MAX_PAIRS, log: Log | None = None) -> tuple[float | None, dict]:
    """The buckets that moved, and by how much: the largest move, and `{bucket: move}`.

    `drift_span` only sees cues already within 0.25 s of the global offset, so a segment that jumps
    drops out rather than disagreeing. A 5-minute bucket counts as *moved* when fewer than
    `MIN_MATCH_RATIO` of its cues land at the global offset **and** at least `MOVED_MATCH_RATIO`
    land at one other offset within ±`LOCAL_WINDOW_S` (the same histogram estimator). Both halves
    matter: a free local estimate on its own locks onto false peaks in evenly spaced dialogue, and a
    bucket that lands nowhere (an opening or recap one side lacks) is evidence of nothing.
    Measured: Shahid rips of LxL ep 4 move +1.43..+1.49 s at 0.54-0.71 after ~15
    min; Thistle ep 44's cross-source pair moves +4.68 s and +8.38 s at 0.94; the good pairs never do.
    """
    buckets: dict[int, list[float]] = {}
    for c in donor:
        buckets.setdefault(int(c // DRIFT_BUCKET_S), []).append(c + offset)
    moved: dict[int, float] = {}
    looked = [(k, cues) for k, cues in sorted(buckets.items())
              if len(cues) >= DRIFT_MIN_CUES * 2 and _landed(cues, target) < MIN_MATCH_RATIO]
    # One pair budget for all the buckets, shared by their pair counts: one budget each would add up past it.
    counts = [_thinned(cues, target, LOCAL_WINDOW_S, max_pairs)[2] for _, cues in looked]
    total = sum(counts)
    if total > max_pairs and log is not None:
        log.warn("subs.offset", "sample", result="WARN", pairs=total, limit=max_pairs, buckets=len(looked),
                 window=LOCAL_WINDOW_S, suggest=f"the per-bucket check compares {total} cue pairs, past the "
                                                f"{max_pairs} limit; each bucket reads a share of its cues")
    for (k, cues), n in zip(looked, counts):
        share = max_pairs if total <= max_pairs else max(1, max_pairs * n // total)
        res, ratio = estimate_offset(cues, target, LOCAL_WINDOW_S, max_pairs=share)
        if ratio >= MOVED_MATCH_RATIO and abs(res) > MAX_DRIFT_SPAN_S:
            moved[k] = round(res, 3)
    if not moved:
        return None, moved
    return round(max(abs(v) for v in moved.values()), 3), moved


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def offset_decision(donor_cues: list[float], target_cues: list[float], max_offset: float,
                    log: Log | None = None) -> dict:
    """The whole item-3 judgement in one pure function: estimate, then the three guards. Each of the two
    histogram stages compares at most ALIGN_MAX_PAIRS cue pairs (`log` gets the line when one is thinned)."""
    if max_offset <= 0:
        return {"offset": 0.0, "match_ratio": None, "bucket_span": None, "bucket_medians": {},
                "timing_verified": False, "reason": "retiming disabled (subtitleSyncMaxOffset = 0)"}
    offset, ratio = estimate_offset(donor_cues, target_cues, max_offset, log=log)
    span, medians = drift_span(donor_cues, target_cues, offset)
    jump, local = local_offsets(donor_cues, target_cues, offset, log=log)
    reason = ""
    if ratio < MIN_MATCH_RATIO:
        reason = (f"only {ratio:.0%} of the donor's cues land on the target's at {offset:+.3f} s "
                  f"(need {MIN_MATCH_RATIO:.0%}) — most likely a different episode")
    elif abs(offset) > max_offset:
        reason = f"offset {offset:+.3f} s is outside the +-{max_offset:g} s search window"
    elif span is not None and span > MAX_DRIFT_SPAN_S:
        reason = (f"the residual drifts {span:.3f} s across 5-minute buckets (limit "
                  f"{MAX_DRIFT_SPAN_S}) — a framerate stretch or an ad-break split, which one "
                  f"constant cannot fix")
    elif jump is not None and jump > MAX_DRIFT_SPAN_S:
        reason = (f"the timing jumps mid-episode: from minute {min(local) * 5} the cues land "
                  f"{', '.join(f'{v:+.2f}' for v in local.values())} s off the rest — a different cut, "
                  f"which one constant cannot fix")
    if reason:
        return {"offset": 0.0, "match_ratio": ratio, "bucket_span": span, "bucket_medians": medians,
                "timing_verified": False, "reason": reason, "local_offsets": local}
    return {"offset": offset, "match_ratio": ratio, "bucket_span": span, "bucket_medians": medians,
            "timing_verified": True, "reason": "", "local_offsets": local}


def estimate_from_anchors(log: Log, donor: dict, chosen: dict, donor_text: str, donor_fmt: str,
                          settings: dict, track_source: SubtitleTracks | None = None) -> dict:
    """Download both releases' anchor tracks (through `track_source`, each from its own release's indexer),
    estimate the offset, and return the decision.

    Same streaming source -> offset 0 with no anchor fetch at all: measured, Kumo-raws `ara` lands on
    the SubsKindly release's own `eng` grid 213/213 exact, median 0.000 s, max |delta| 0.000 s. Two
    extra HTTP GETs to learn nothing is not worth 0.7 s on the play path.
    """
    t0 = time.monotonic()
    max_offset = float(as_int(settings.get("subtitleSyncMaxOffset"), 60))
    chosen_parsed = chosen.get("parsed") or parse_title(str(chosen.get("title") or ""))
    chosen_source = streaming_source(chosen_parsed)
    donor_source = streaming_source({"source": donor.get("source")})
    same_source = bool(chosen_source) and chosen_source == donor_source
    base = {"offset": 0.0, "match_ratio": None, "bucket_span": None, "bucket_medians": {},
            "anchor_lang": donor.get("anchor_lang"), "applied": "rewrite"}
    if same_source:
        out = {**base, "timing_verified": True, "reason": "same streaming source; measured zero offset"}
        log.info("subs.offset", "skip", result="PASS", ms=(time.monotonic() - t0) * 1000,
                 source=chosen_source, **{k: out[k] for k in ("offset", "timing_verified")})
        return out
    anchor = donor.get("anchor_lang")
    if not anchor or max_offset <= 0:
        out = {**base, "timing_verified": False,
               "reason": ("retiming disabled (subtitleSyncMaxOffset = 0)" if max_offset <= 0 else
                          "the two releases share no anchor language; loaded unshifted")}
        log.warn("subs.offset", "no-anchor", result="WARN", ms=(time.monotonic() - t0) * 1000,
                 anchor_lang=anchor, missing="anchor-language", reason=out["reason"],
                 suggest="nudge it from the popout, or `omarchy-anime player sub-delay <seconds>`")
        return out
    donor_anchor = _anchor_track({"tracks": donor.get("_tracks")}, anchor)
    target_anchor = _anchor_track(chosen, anchor)
    if not donor_anchor or not target_anchor:
        # Which side lacked the record is the whole diagnosis, and deriving it from the code
        # afterwards cost a full trace read once already.
        missing = "both" if not donor_anchor and not target_anchor else ("donor" if not donor_anchor else "target")
        out = {**base, "timing_verified": False, "reason": f"no {anchor} attachment on both sides"}
        log.warn("subs.offset", "no-anchor", result="WARN", anchor_lang=anchor, missing=missing,
                 reason=out["reason"],
                 target_tracks=len(chosen.get("tracks") or []), donor_tracks=len(donor.get("_tracks") or []),
                 suggest=f"the {missing} release has no downloadable {anchor} attachment record; "
                         f"the donor is loaded unshifted")
        return out
    a = fetch_track(log, track_source, {**release_key(donor).as_dict(), "track_id": donor_anchor["id"],
                                        "format": donor_anchor["format"], "url": donor_anchor["url"], "lang": anchor})
    b = fetch_track(log, track_source, {**tracks_key(chosen).as_dict(), "track_id": target_anchor["id"],
                                        "format": target_anchor["format"], "url": target_anchor["url"], "lang": anchor})
    if not a or not b:
        out = {**base, "timing_verified": False, "reason": f"an {anchor} anchor track could not be downloaded"}
        log.warn("subs.offset", "anchor-fetch", result="WARN", reason=out["reason"])
        return out
    donor_cues = [c for c, _ in parse_cues(a["text"], a["format"], log, a["path"])]
    target_cues = [c for c, _ in parse_cues(b["text"], b["format"], log, b["path"])]
    decision = offset_decision(donor_cues, target_cues, max_offset, log)
    out = {**base, **decision, "anchor_lang": anchor}
    log.emit("info" if out["timing_verified"] else "warn", "subs.offset", "estimate",
             result="PASS" if out["timing_verified"] else "WARN", ms=(time.monotonic() - t0) * 1000,
             anchor_lang=anchor, offset=out["offset"], match_ratio=out["match_ratio"],
             bucket_span=out["bucket_span"], bucket_medians=out["bucket_medians"],
             donor_cues=len(donor_cues), target_cues=len(target_cues), applied="rewrite",
             suggest=out.get("reason") or "")
    return out


def _anchor_track(release: dict, lang: str) -> dict | None:
    for t in release.get("tracks") or []:
        if normalize_lang(t.get("lang")) == normalize_lang(lang) \
                and (t.get("format") or "").upper() in SUBTITLE_FORMATS \
                and not t.get("forced") and t.get("url"):
            return t
    return None


# ----------------------------------------------------------------------------- what reaches the player

# With mpv's `embeddedfonts=yes`, libass loads a font out of an ASS file's `[Fonts]` and reads font data until one of
# the four headers it knows (libass `ass.c` `process_line`: the line's BOMs, then spaces and tabs, skipped; the name
# matched as an ASCII case-insensitive prefix). `[Graphics]` it does not know. A file the plugin writes loses both
# sections, each up to such a header or a `Dialogue:`/`Comment:` line (kept: ffmpeg's `ass` demuxer takes a
# `Dialogue:` line wherever it sits, and with the section's header gone libass never reads it as font data).
# Only ASS text is cut: mpv opens a file as ASS only when it starts with `[Script Info]` (ffmpeg's `ass` probe, after
# a byte-order mark and blank lines; the demuxer has no file-extension fallback, and a lowercase header opened as
# `lrc`, measured with mpv 0.41 / ffmpeg n9.0.2). A `[Graphics]` caption in an SRT is text mpv shows.
_ATTACHMENT_HEADERS = ("[fonts]", "[graphics]")
_LIBASS_HEADERS = ("[script info]", "[v4 styles]", "[v4+ styles]", "[events]")
_SECTION_ENDS = _LIBASS_HEADERS + ("dialogue:", "comment:")
_ATTACHMENT_HINT = re.compile(r"\[(?:fonts|graphics)\]", re.I)
_LINE_BREAK = re.compile(r"(\r\n|\r|\n)")


def strip_ass_attachments(text: str) -> tuple[str, list[str], int]:
    """`(text without its [Fonts] and [Graphics] sections, the headers cut, characters removed)` for ASS text,
    whatever the format guess said (mpv probes the content, not the name); any other text comes back as it is."""
    if not _ATTACHMENT_HINT.search(text) \
            or not text.lstrip("\ufeff \t\r\n")[:13].lower().startswith("[script info]"):
        return text, [], 0
    parts = _LINE_BREAK.split(text)             # line, break, line, break, ..., line
    keep: list[str] = []
    cut: list[str] = []
    dropping = False
    for i in range(0, len(parts), 2):
        head = parts[i].lstrip("\ufeff").lstrip(" \t")[:16].lower()
        if head.startswith(_ATTACHMENT_HEADERS):
            dropping = True
            cut.append(head.partition("]")[0] + "]")
        elif head.startswith(_SECTION_ENDS):
            dropping = False
        if not dropping:
            keep.append(parts[i] + (parts[i + 1] if i + 1 < len(parts) else ""))
    out = "".join(keep)
    return out, cut, len(text) - len(out)


# ffmpeg's `ass` probe, measured with ffprobe n9.0.2 on 2026-11-01: one byte-order mark and any CR/LF are skipped, then
# the bytes must be `[Script Info]` exactly. A space or tab before it, a `;` line first or another case fails (a case
# variant opens as `lrc`), and mpv then refuses the file in `sub-add`. An uploader's banner in place of that line
# (one cached file in 33) is moved below the header as comments. ASS text is told by a line libass reads
# (`_ASS_CONTENT`); the first such line ends the banner (`_ASS_LINE`: a section header, or a `[Script Info]` field
# named in the ASS spec, so a banner's `https://` or `Ripped by: x` is not one). An SRT's caption `[Events]` is not
# a reason: WebVTT, text with a `-->` line before its first ASS line, or a banner over `REPAIR_MAX_LINES`, is left.
_ASS_PROBE = "[Script Info]"
_VTT_PROBE = "WEBVTT"                           # ffmpeg's `webvtt` probe: these bytes, after the same skips
_ASS_CONTENT = re.compile(r"^[\ufeff \t]*(?:\[(?:v4\+? styles|events)\]|scripttype[ \t]*:)", re.I | re.M)
_ASS_SECTIONS = _LIBASS_HEADERS + _ATTACHMENT_HEADERS + ("[aegisub project garbage]", "[aegisub extradata]")
_ASS_FIELD = re.compile(r"(?:scripttype|title|playres[xy]|playdepth|wrapstyle|scaledborderandshadow|collisions|timer"
                        r"|original (?:script|translation|editing|timing)|synch point|script updated by"
                        r"|update details|ycbcr matrix|layoutres[xy]|kerning|language)[ \t]*:", re.I)
REPAIR_MAX_LINES = 20


def _ass_line(line: str) -> bool:
    head = line.lstrip("\ufeff \t")
    return head.lower().startswith(_ASS_SECTIONS) or bool(_ASS_FIELD.match(head))


def repair_ass_header(text: str) -> tuple[str, int, str]:
    """`(text, banner lines turned into comments, why)`. ASS text that ffmpeg's probe would not open as ASS comes back
    with `[Script Info]` as its first line and the lines before its first ASS line as `; ` comments below it (a case
    or space variant of the header line is rewritten, not doubled), in the file's own line breaks. Text the probe
    opens, text with no ASS content, WebVTT (a first line `WEBVTT`) and an SRT (a line with `-->` before the first ASS
    line) come back as they are with `why` empty; a banner over `REPAIR_MAX_LINES` comes back as it is with `why`
    saying so."""
    head = text[1:] if text.startswith("\ufeff") else text
    if head.lstrip("\r\n").startswith((_ASS_PROBE, _VTT_PROBE)) or not _ASS_CONTENT.search(text):
        return text, 0, ""
    parts = _LINE_BREAK.split(text)             # line, break, line, break, ..., line
    lines = parts[0::2]
    first = next((i for i, line in enumerate(lines) if _ass_line(line)), None)
    if first is None:                           # never: each `_ASS_CONTENT` line is an ASS line; no StopIteration
        return text, 0, ""
    banner = lines[:first]
    written = sum(1 for b in banner if b.strip("\ufeff \t"))
    if written > REPAIR_MAX_LINES:
        return text, 0, f"left as it is: {written} lines before the first ASS line (over {REPAIR_MAX_LINES})"
    if any("-->" in b for b in banner):                 # an SRT whose caption reads `[Events]`
        return text, 0, ""
    brk = parts[1] if len(parts) > 1 else "\n"
    is_header = lines[first].lstrip("\ufeff \t").lower().startswith("[script info]")
    if not is_header:
        why = "no [Script Info] line before the file's sections"
    elif written:
        why = "the [Script Info] line is not the file's first line"
    else:
        why = "the [Script Info] line is not written as the probe reads it (case or leading space)"
    out = [_ASS_PROBE + (lines[first].lstrip("\ufeff \t")[13:] if is_header else "") + brk]
    commented = 0
    for i, line in enumerate(banner):
        bare = line.strip("\ufeff \t")
        if bare and not bare.startswith(";"):
            line, commented = "; " + bare, commented + 1
        out.append(line.lstrip("\ufeff") + parts[2 * i + 1])
    out.append("".join(parts[2 * (first + 1 if is_header else first):]))
    return "".join(out), commented, why


# ----------------------------------------------------------------------------- cache layout

def release_dir(info_hash: str | None, anidb_eid=None) -> Path:
    """Keyed by the **played** release's infohash, so a replay is instant and a different release of
    the same episode recomputes its own offset."""
    key = (info_hash or "").lower() or (f"eid-{anidb_eid}" if anidb_eid else "unkeyed")
    return SUBS_DIR / _SAFE.sub("_", key)


def cache_path(info_hash: str | None, lang: str, donor: dict, fmt: str, anidb_eid=None) -> Path:
    """`<lang>-<donor source or group>.<ext>`. A generic source token (`WEB`, `BD`) names nothing the
    user would recognise, so the group wins there — `ara-Zafir.srt`, not `ara-WEB.srt`."""
    source = str(donor.get("source") or "").upper()
    tag = donor.get("source") if source not in GENERIC_SOURCES else (donor.get("group") or donor.get("source"))
    tag = _SAFE.sub("-", str(tag or donor.get("release_id") or "x")).strip("-")
    return release_dir(info_hash, anidb_eid) / f"{lang}-{tag or 'x'}{EXT_FOR_FORMAT.get(fmt.upper(), '.srt')}"


def write_track(log: Log, info_hash: str | None, lang: str, donor: dict, fetched: dict,
                text: str | None = None, meta_extra: dict | None = None, anidb_eid=None,
                trace: str = "") -> dict:
    """Place a (possibly retimed) track next to its `meta.json` for this release. Atomic."""
    path = cache_path(info_hash, lang, donor, fetched["format"], anidb_eid)
    payload, cut, removed = strip_ass_attachments(text if text is not None else fetched.get("text") or "")
    if cut:
        log.warn("subs.cache", "strip", result="WARN", path=str(path), sections=cut, chars_removed=removed,
                 suggest="the track's embedded fonts/pictures were left out; its styles name system fonts")
    write_bytes_atomic(path, payload.encode(WRITTEN_ENCODING))
    meta = {
        "lang": lang,
        "donor": {k: donor.get(k) for k in DONOR_KEYS},
        "track_id": donor.get("track_id"), "format": fetched["format"],
        "forced": bool(donor.get("forced")), "tier": donor.get("tier"), "tier_name": donor.get("tier_name"),
        "anchor_lang": donor.get("anchor_lang"),
        "offset": 0.0, "match_ratio": None, "residual_median": None, "bucket_span": None,
        "timing_verified": None, "encoding": fetched["encoding"], "bytes": len(payload.encode(WRITTEN_ENCODING)),
        "raw": fetched["path"], "fetched_at": time.time(), "trace": trace,
    }
    meta.update(meta_extra or {})
    write_cache(release_dir(info_hash, anidb_eid) / "meta.json", meta)
    log.info("subs.cache", "write", result="PASS", path=str(path), lang=lang, bytes=meta["bytes"],
             encoding=fetched["encoding"], written=WRITTEN_ENCODING,
             donor=donor.get("title"), offset=meta["offset"], timing_verified=meta["timing_verified"])
    return {"path": str(path), "meta": meta}


def read_meta(info_hash: str | None, anidb_eid=None) -> dict | None:
    return cached_json(release_dir(info_hash, anidb_eid) / "meta.json", None)


# ----------------------------------------------------------------------------- the stage

def language_name(lang: str) -> str:
    """"ara" -> "Arabic", for the track title mpv shows and the badge the popout draws (`core.LANGUAGE_NAMES`,
    not the longest title word, which would call Malay `Melayu`), and `core`'s one rule for a code it does not hold."""
    return core_language_name(lang)


PREPARE_BUDGET_S = 3.0


def prepare(http: Http, log: Log, releases: list[dict], chosen: dict, settings: dict, *,
            episode: int, alt=None, anidb_eid=None, observed_langs: list[str] | None = None,
            budget_s: float = PREPARE_BUDGET_S, trace: str = "",
            track_source: SubtitleTracks | None = None) -> dict | None:
    """Find, download, retime and cache a side-loaded track for `chosen`. Never raises.

    Returns `{path, lang, donor, offset, timing_verified, …}` for `sub-add`, or `None` plus a logged
    reason — in which case playback is exactly what it is today. Each step is separately
    capped and the whole stage is bounded by `budget_s`. `track_source` is the subtitle-track capability
    (`indexers.SubtitleTracks`); when no loaded add-on offers it, no donor is looked for.
    """
    t0 = time.monotonic()
    try:
        return _prepare(http, log, releases, chosen, settings, episode=episode, alt=alt, anidb_eid=anidb_eid,
                        observed_langs=observed_langs, budget_s=budget_s, trace=trace, track_source=track_source)
    except Exception as exc:                       # noqa: BLE001 - it runs after the stream loaded
        log.warn("subs.sideload", "done", result="FAIL", sub_source="none", error=f"{type(exc).__name__}: {exc}",
                 traceback=traceback.format_exc()[-1500:], ms=(time.monotonic() - t0) * 1000,
                 reason="the side-load raised", suggest=f"playing without a side-loaded track; bug: report with "
                 f"`omarchy-anime trace {log.trace}`")
        return None


def _prepare(http: Http, log: Log, releases: list[dict], chosen: dict, settings: dict, *,
             episode: int, alt, anidb_eid, observed_langs: list[str] | None, budget_s: float, trace: str,
             track_source: SubtitleTracks | None) -> dict | None:
    """`prepare`'s body, which may raise; `prepare` turns anything it raises into one FAIL line and None."""
    t0 = time.monotonic()
    lang = settings["subtitleLanguage"]
    info_hash = chosen.get("info_hash")
    log.info("subs.sideload", "start", lang=lang, chosen_id=chosen.get("id"), info_hash=info_hash,
             policy=settings.get("subtitlePolicy"), budget_s=budget_s,
             observed=sorted(set(observed_langs or [])) or None)

    def done(result: str, source: str, reason: str = "", **extra):
        log.emit("info" if result == "PASS" else "warn", "subs.sideload", "done", result=result,
                 ms=(time.monotonic() - t0) * 1000, sub_source=source, reason=reason,
                 suggest=reason if result != "PASS" else "", **extra)

    # mpv's own track-list is the best statement of what the played file contains; it makes the
    # anchor language known without an index lookup.
    if observed_langs:
        chosen["verified_langs"] = list(observed_langs)

    # A replay of the same release is a local read: the retimed file is keyed by its infohash.
    meta = read_meta(info_hash, anidb_eid)
    if meta and meta.get("lang") == lang and misread(meta.get("encoding"), lang):
        # An older build read every non-UTF-8 track as cp1256 and wrote that text here. The raw
        # track is normally still cached, so reading it again costs the donor scan and no download; it is
        # downloaded once, as on a first play, when the top donor has changed or `prune` evicted the raw file.
        # The garbled files go first: a rebuild from another donor writes another name, and the replay globs
        # `<lang>-*`.
        removed = _drop_tracks(log, info_hash, anidb_eid, lang)
        log.info("subs.sideload", "stale", result="SKIP", removed=removed, encoding=meta.get("encoding"),
                 lang=lang, reason=f"the cached track was read as {meta.get('encoding')}, not a {lang} codepage")
        meta = None
    if meta and meta.get("lang") == lang:
        cached = release_dir(info_hash, anidb_eid) / Path(meta.get("path") or "").name if meta.get("path") else None
        cached = cached or _existing_track(info_hash, anidb_eid, lang)
        if cached and cached.exists() and cached.stat().st_size > 0:
            # Only today's donor keys leave the file: an older record names its
            # donor by an older key, which nothing reads any more (title/group/source still show).
            donor = {k: (meta.get("donor") or {}).get(k) for k in DONOR_KEYS}
            out = {"path": str(cached), "lang": lang, "donor": donor,
                   "offset": meta.get("offset") or 0.0, "timing_verified": meta.get("timing_verified"),
                   "anchor_lang": meta.get("anchor_lang"), "match_ratio": meta.get("match_ratio"),
                   "forced": bool(meta.get("forced")), "format": meta.get("format"),
                   "tier_name": meta.get("tier_name"), "from_cache": True}
            done("PASS", "sideload", cached=True, path=str(cached))
            return out

    if track_source is not None and not track_source.offered:
        done("SKIP", "none", "no loaded indexer add-on offers subtitle tracks, so no release can donate one")
        return None
    try:
        scan = find_donor(http, log, releases, chosen, settings, episode=episode, alt=alt,
                          budget_s=min(DONOR_BUDGET_S, budget_s), track_source=track_source)
    except Exception as exc:                       # noqa: BLE001 - the play path must not die here
        done("FAIL", "none", f"the donor scan failed: {exc}")
        return None
    if not scan["donors"]:
        done("FAIL", "none", f"no release of this episode carries a {lang} track "
                             f"({scan['examined']} read)", examined=scan["examined"])
        return None
    donor = scan["donors"][0]
    log.info("subs.donor", "pick", result="PASS", indexer=donor["indexer"], release_id=donor["release_id"],
             title=donor["title"], group=donor["group"], source=donor["source"], tier=donor["tier"],
             tier_name=donor["tier_name"], seeders=donor["seeders"], track_id=donor["track_id"],
             forced=donor["forced"], anchor_lang=donor["anchor_lang"])
    if time.monotonic() - t0 > budget_s:
        done("FAIL", "none", "the side-load budget ran out during the donor scan")
        return None

    fetched = fetch_track(log, track_source, donor)
    if not fetched:
        done("FAIL", "none", f"the {lang} track of {donor['title']} could not be downloaded")
        return None

    est = estimate_from_anchors(log, donor, chosen, fetched["text"], fetched["format"], settings,
                                track_source=track_source)
    text = shift_text(fetched["text"], fetched["format"], est["offset"])
    try:
        placed = write_track(log, info_hash, lang, donor, fetched, text=text,
                             meta_extra={k: est[k] for k in ("offset", "match_ratio", "bucket_span",
                                                             "timing_verified", "anchor_lang", "applied")},
                             anidb_eid=anidb_eid, trace=trace)
    except OSError as exc:
        done("FAIL", "none", f"the retimed file could not be written: {exc}")
        return None
    out = {"path": placed["path"], "lang": lang, "donor": donor, "offset": est["offset"],
           "timing_verified": est["timing_verified"], "anchor_lang": est.get("anchor_lang"),
           "match_ratio": est.get("match_ratio"), "forced": donor["forced"], "format": fetched["format"],
           "tier_name": donor["tier_name"], "from_cache": False, "reason": est.get("reason") or ""}
    done("PASS", "sideload", offset=out["offset"], timing_verified=out["timing_verified"],
         donor=donor["title"], examined=scan["examined"])
    return out


def _drop_tracks(log: Log, info_hash, anidb_eid, lang: str) -> list[str]:
    """Remove this release's `<lang>-*` retimed files (re-derivable from the raw cache). Never raises: a file
    it cannot remove is logged, and the replay's glob may still find it."""
    removed = []
    for p in sorted(release_dir(info_hash, anidb_eid).glob(f"{lang}-*")):     # a glob never raises
        try:
            if p.is_file():
                p.unlink()
                removed.append(p.name)
        except OSError as e:
            log.warn("subs.sideload", "stale-remove", result="FAIL", path=str(p), error=str(e),
                     suggest="the garbled file is left, and the replay can serve it; remove it by hand")
    return removed


def _existing_track(info_hash, anidb_eid, lang: str) -> Path | None:
    try:
        for p in sorted(release_dir(info_hash, anidb_eid).glob(f"{lang}-*")):
            if p.is_file():
                return p
    except OSError:
        pass
    return None


def accept_readback(readback: dict, lang: str) -> bool:
    """`sub-add` answering `success` is not proof. Only the language mpv reports as selected
    afterwards decides whether the side-load worked."""
    return same_language(readback.get("lang"), lang)


def now_playing_keys(sideload: dict | None, sub_source: str, sub_lang: str | None, lang: str,
                     provider: str = "") -> dict:
    """The `sub*` half of now-playing.json. Pure, so the popout's contract is unit-testable.

    `subFallback` keeps its current meaning ("not the preferred language") and therefore becomes
    False when a side-load succeeds, so the existing popout line degrades correctly even before the
    QML knows about `subSource`.
    """
    donor = (sideload or {}).get("donor") or {}
    return {
        "subSource": sub_source,
        "subOffset": round(float(sideload["offset"]), 3) if sideload else None,
        "subTimingVerified": sideload.get("timing_verified") if sideload else None,
        "subDonor": donor.get("title"),
        "subDonorSource": donor.get("source") or donor.get("group") or None,
        "subAnchor": sideload.get("anchor_lang") if sideload else None,
        "subForced": bool(sideload.get("forced")) if sideload else False,
        "subBadge": badge(sideload, sub_lang, lang, provider),
    }


def badge(sideload: dict | None, sub_lang: str | None, lang: str, provider: str = "") -> str:
    """The one line the popout and the overlay show about where the subtitles came from."""
    name = language_name(lang)
    if sideload:
        donor = (sideload.get("donor") or {})
        who = donor.get("group") or donor.get("title") or "another release"
        if not str(who).startswith("["):
            who = f"[{who}]"
        if sideload.get("forced"):
            return f"{name} (signs only) added from {who}"
        if sideload.get("timing_verified") and sideload.get("offset"):
            return f"{name} added from {who} · shifted {sideload['offset']:+.1f} s"
        if sideload.get("timing_verified"):
            src = donor.get("source")
            return f"{name} added from {who}" + (f" · same {src} source" if src else "")
        return f"{name} added from {who} · timing unverified — nudge it below"
    if sub_lang and same_language(sub_lang, lang):
        # Named, not tagged: `Arabic` for `ar-SA`, `Portuguese (Brazil)` for `pt-BR`.
        return f"Subtitles: {core_language_name(sub_lang)}" + (f" · {provider}" if provider else "")
    if sub_lang:
        # The track on screen by its name (`English`, the second language's), its tag when unknown.
        return (f"No {name} subtitles · playing with {core_language_name(sub_lang)} — the source platform has "
                f"no {name} for this title")
    return f"No {name} subtitles for this episode"


# ----------------------------------------------------------------------------- housekeeping

def _files() -> list[Path]:
    out: list[Path] = []
    try:
        for p in SUBS_DIR.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                out.append(p)
    except OSError:
        return []
    return out


def prune(max_age_days: float = PRUNE_MAX_AGE_DAYS, max_bytes: int = PRUNE_MAX_BYTES) -> dict:
    """Same policy shape as `covers.prune`: delete what has been untouched for `max_age_days`, then,
    if the directory is still over `max_bytes`, the least recently used until it is not.

    `raw/` is exempt from the age rule and evicted last by the size rule, for the same reason: a
    retimed file can be re-derived from a raw one with no network call, so the derived copy is
    strictly the cheaper thing to lose. `subs prune --max-age-days 0` therefore empties the retimed
    directories and leaves the originals.
    """
    t0 = time.monotonic()
    removed, freed = 0, 0
    cutoff = time.time() - max_age_days * 86400
    alive: list[tuple[float, int, Path, bool]] = []
    for p in _files():
        try:
            st = p.stat()
        except OSError:
            continue
        is_raw = RAW_DIR in p.parents
        # `raw/` is the immutable original, `raw/<indexer>/<track id>`: a retimed file can always be
        # re-derived from it with no network call, so age alone never evicts it. Only the size cap
        # does, and only after every derived copy is gone.
        if not is_raw and st.st_mtime < cutoff and st.st_atime < cutoff:
            try:
                p.unlink()
                removed += 1
                freed += st.st_size
                continue
            except OSError:
                pass
        alive.append((max(st.st_atime, st.st_mtime), st.st_size, p, is_raw))
    total = sum(size for _, size, _, _ in alive)
    if total > max_bytes:
        for _, size, p, _ in sorted(alive, key=lambda t: (t[3], t[0])):   # derived before raw, LRU first
            if total <= max_bytes:
                break
            try:
                p.unlink()
            except OSError:
                continue
            removed += 1
            freed += size
            total -= size
    _drop_empty_dirs()
    return {"removed": removed, "freed": freed, "count": len(_files()),
            "bytes": sum(p.stat().st_size for p in _files() if p.exists()),
            "ms": round((time.monotonic() - t0) * 1000, 1)}


def _drop_empty_dirs() -> None:
    try:
        for d in sorted(SUBS_DIR.glob("*"), reverse=True):
            if d.is_dir() and d != RAW_DIR and not any(d.iterdir()):
                d.rmdir()
    except OSError:
        pass


def maybe_prune(log: Log | None = None, settings: dict | None = None) -> dict | None:
    """At most once a day, behind a stamp file, exactly like `covers.maybe_prune`."""
    stamp = SUBS_DIR / STAMP_NAME
    try:
        if time.time() - stamp.stat().st_mtime < PRUNE_EVERY_S:
            return None
    except OSError:
        pass
    core.touch_private(stamp)
    # The line says which limit it cut to and where that came from, as `subs prune`'s does.
    given = (settings or {}).get("subsCacheMaxBytes")
    limit = PRUNE_MAX_BYTES if given is None else as_int(given, PRUNE_MAX_BYTES)
    out = prune(max_bytes=limit)
    if log is not None:
        log.info("subs.prune", "daily", result="PASS", max_bytes=limit,
                 max_bytes_from="default" if given is None else "subsCacheMaxBytes", **out)
    return out


def status() -> dict:
    files = _files()
    rows = []
    for p in files:
        try:
            rows.append((p.stat().st_mtime, p.stat().st_size, str(p)))
        except OSError:
            continue
    raw = [r for r in rows if f"{os.sep}raw{os.sep}" in r[2]]
    return {"dir": str(SUBS_DIR), "count": len(rows), "bytes": sum(s for _, s, _ in rows),
            "raw_count": len(raw), "raw_bytes": sum(s for _, s, _ in raw),
            "releases": sorted({str(Path(r[2]).parent.name) for r in rows if r not in raw}),
            "oldest": min((m for m, _, _ in rows), default=None),
            "newest": max((m for m, _, _ in rows), default=None)}
