"""External subtitles: Subdl and OpenSubtitles, for an episode no release translates.

Terms: an **external subtitle** is a file from a subtitle database, as opposed to a
**donor** track borrowed from another release of the same episode. Donors stay first; this module is
asked only when no release carries the viewer's language or the donor's timing is unverified.

The constants here were first measured against Subdl and OpenSubtitles on 2026-09-26; a later measurement is dated
beside the constant it set.
"""
from __future__ import annotations

import io
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import core
from .anizip import airdate, movie_sources, movie_pairs as anizip_movie_pairs, pos_int as _pos_int
from .core import Http, Log, StageError, cached_json, get_capped, read_token, write_bytes_atomic, write_cache
from .indexers import SUBTITLE_FORMATS, release_key
from .score import SERIES_SIMILARITY, parse_title, season_number, series_similarity, sibling_name
from .subs import (WRITTEN_ENCODING, decode_text, estimate_offset, offset_decision, parse_cues, shift_text,
                   streaming_source, strip_ass_attachments)

# The offset window for an external subtitle. Measured: an `[PTQ-plumsubs]` BD file for LxL
# ep 4 sits at +80.15 s, constant; donors keep `subtitleSyncMaxOffset` (60).
EXTERNAL_MAX_OFFSET_S = 180.0
TOP_N = 3                                   # the best three load as tracks

SERVICES = ("opensubtitles", "subdl")
SUBDL_SEARCH = "https://api.subdl.com/api/v1/subtitles"
SUBDL_DL = "https://dl.subdl.com"
OS_API = "https://api.opensubtitles.com/api/v1"
# Per-host spacing. OpenSubtitles answers `ratelimit-limit: 5` per second (measured); Subdl publishes
# none, and the measurement's 1.1 s saw no 429 in 458 requests.
PACING_S = {"subdl": 1.0, "opensubtitles": 0.25}
# A service that asks for a longer wait than this is skipped for the play, never slept for.
RETRY_AFTER_CAP_S = 10
MAX_PAGES = 3

# ISO 639-2 -> (Subdl `languages`, OpenSubtitles `languages`). Verified 2026-09-26: OpenSubtitles'
# /infos/languages (Portuguese is `pt-pt` and `pt-br`); Subdl's codes as its answers spell them
# (`BR_PT` seen; `TR` and `PT` accepted without error, no file seen yet).
# The other languages of `core.LANGUAGE_TABLE`. OpenSubtitles from its /infos/languages (2026-10-04,
# no key); Subdl from its `language_list.json` (behind Cloudflare: a committed copy of 2024-05 and Bazarr's `subdl`
# converter, whose `GL` and `EU` the copy lacks). Chinese is both scripts: Subdl `ZH` and `ZH_BG` (Big 5), OpenSubtitles
# `zh-cn` and `zh-tw`. Filipino is Tagalog on both. None: the service has no code for it, and is not asked.
LANG_CODES = {
    "ara": ("AR", "ar"), "eng": ("EN", "en"), "spa": ("ES", "es"), "por": ("PT,BR_PT", "pt-pt,pt-br"),
    "fre": ("FR", "fr"), "ger": ("DE", "de"), "ita": ("IT", "it"), "rus": ("RU", "ru"),
    "tur": ("TR", "tr"), "pol": ("PL", "pl"), "per": ("FA", "fa"),
    "chi": ("ZH,ZH_BG", "zh-cn,zh-tw"), "ind": ("ID", "id"), "tha": ("TH", "th"), "may": ("MS", "ms"),
    "vie": ("VI", "vi"), "jpn": ("JA", "ja"), "kor": ("KO", "ko"), "dan": ("DA", "da"), "hun": ("HU", "hu"),
    "fin": ("FI", "fi"), "dut": ("NL", "nl"), "swe": ("SV", "sv"), "heb": ("HE", "he"), "gre": ("EL", "el"),
    "cze": ("CS", "cs"), "rum": ("RO", "ro"), "nor": ("NO", "no"), "hrv": ("HR", "hr"), "fil": ("TL", "tl"),
    "ukr": ("UK", "uk"), "hin": ("HI", "hi"), "cat": ("CA", "ca"), "glg": ("GL", "gl"), "sin": ("SI", "si"),
    "baq": ("EU", "eu"), "bos": ("BS", "bs"), "slo": ("SK", "sk"),
}
# Which slot of `LANG_CODES` each service reads: the query, the answer's filter and the skip of a service with no code
# for the language all index by it.
CODE_SLOT = {"subdl": 0, "opensubtitles": 1}
# The variants' own codes, in `LANG_CODES`' slots: a viewer who chose one is asked for it first, then for the
# language's codes; "" where a service has none. OpenSubtitles /infos/languages (2026-10-27, no key): `pt-br`,
# `pt-pt`, `zh-cn`, `zh-tw`, `ea` "Spanish (LA)", `sp` "Spanish (EU)". Subdl (Bazarr's `subdl` converter; the list
# itself is behind Cloudflare): `BR_PT`, `PT`, `ZH` (GB), `ZH_BG` (Big 5), and one Spanish, `ES`.
VARIANT_CODES = {
    "es-419": ("", "ea"), "es-ES": ("", "sp"), "pt-BR": ("BR_PT", "pt-br"), "pt-PT": ("PT", "pt-pt"),
    "zh-Hans": ("ZH", "zh-cn"), "zh-Hant": ("ZH_BG", "zh-tw"),
}


def _variant(lang: str) -> str | None:
    return core.language_variant(lang) if lang not in LANG_CODES else None


def service_codes(lang: str, service: str) -> str:
    """What `service` is asked for `lang`, comma-separated, best first; "" for none. A variant (`pt-BR`) asks its own
    code, then its language's (`pt-br,pt-pt`); a variant this table lacks asks its language's alone."""
    slot = CODE_SLOT[service]
    variant = _variant(lang)
    base = (LANG_CODES.get(core.LANGUAGE_VARIANTS[variant][0] if variant else lang) or ("", ""))[slot] or ""
    own = VARIANT_CODES.get(variant, ("", ""))[slot] if variant else ""
    codes = [own] if own else []
    return ",".join(codes + [c for c in base.split(",") if c and c not in codes])


def _code_owner(service: str, code: str) -> str | None:
    """The variant whose own code on `service` is `code` (`pt-pt` -> `pt-PT`); None for a plain code."""
    slot, code = CODE_SLOT[service], code.lower()
    return next((v for v, codes in VARIANT_CODES.items() if codes[slot] and codes[slot].lower() == code), None)


def region_rank(lang: str, service: str, code: str) -> int:
    """How a file's language `code` answers a viewer who chose a variant: 0 its own, 1 the language's plain code, 2
    another variant's. Always 0 for a viewer who chose none, so a bare viewer's ranking is unchanged."""
    variant = _variant(lang)
    if not variant:
        return 0
    owner = _code_owner(service, code)
    return 0 if owner == variant else 2 if owner else 1


def file_language(lang: str, t: dict) -> str:
    """The language a file is named by: for a variant viewer, the file's own variant (a `pt-pt` file is
    Portuguese (Portugal)), or the language when its code is the plain one; else `lang`."""
    variant = _variant(lang)
    code = str(t.get("code") or "")
    if not variant or not code or t.get("service") not in CODE_SLOT:
        return lang
    return _code_owner(t["service"], code) or core.LANGUAGE_VARIANTS[variant][0]


def known_language(lang: str) -> bool:
    return lang in LANG_CODES or core.language_variant(lang) is not None

# ----------------------------------------------------------------------------- name checks
#
# Every file must name this show and this episode, through the same checks releases pass, or be a
# one-episode record that names no show at all (OpenSubtitles' "S01E004", Subdl's "4.srt"): the
# database's own episode numbers then decide. A name that names a show must name this one: the
# Towakazehiki (Ge) search for TVDB season 5 answers "Kazehiki Series - Rain and Tinder Season",
# which only its name gives away.

_EXT = re.compile(r"(?:\.(?:mkv|mp4|avi))?\.(?:srt|ass|ssa|sub|vtt|zip|txt)$|(?<=\])(?:srt|ass|ssa)$", re.I)
_HASH = re.compile(r"\[[0-9A-Fa-f]{8}\]")
_RES = re.compile(r"\b\d{3,4}p\b|\b4k\b|\b\d{3,4}x\d{3,4}\b", re.I)
_CODEC_NUM = re.compile(r"\b(?:[xh]\s?26[45]|aac\d?(?:\s\d)?|ddp?\d(?:\s\d)?|10-?bits?|8-?bits?|flac\d?(?:\s\d)?)\b", re.I)
# The other release numbers names carry, read as episodes before (measured on
# 2026-09-29). An audio layout after an audio word, spaced (`DDP.5.1`,
# `DTS-HD.MA.5.1`, `TrueHD.7.1`, `Opus 5.1`, `FLAC.2.0`, `Dual Audio 5.1`), loses its digits and keeps the word; a
# glued one (`DDP5.1`, `AAC2.0`) is `_CODEC_NUM`'s. A layout before `ch`/`channels`, a spaced bit depth
# (`10 bits`) and `EAC-3` go whole. A size (`[1.8GB]`) and a dotted version (`[v1.5]`) go whole too, read on the
# dotted form, so `- 05 2GB` keeps its episode; `_` is a separator there (`Title_1.8GB`), as `.` is later. "2.0"
# with no audio word (a title's) stays.
_AUDIO_WORD = (r"(?:ddp?|dd\+|e?-?ac-?3|dts(?:[\s-]?hd)?(?:\s?(?:ma|hra|x))?|truehd(?:\s?atmos)?"
               r"|atmos|opus|flac|aac|lpcm|pcm|mp3|audio)")
_AUDIO_LAYOUT = re.compile(rf"\b({_AUDIO_WORD})\s[1-9]\s[01]\b", re.I)
_RELEASE_NUM = re.compile(r"\b[1-9]\s[01]\s?ch(?:annels?)?\b|\b(?:8|10|12)[\s-]bits?\b|\b(?:e-?)?ac-3\b", re.I)
_SIZE_VERSION = re.compile(r"(?<![^\W_])\d{1,4}(?:[.,]\d{1,2})?\s?[gm]i?b(?![^\W_])"
                           r"|(?<![^\W_])v\d{1,2}\.\d{1,2}(?![^\W_])", re.I)
_RANGE = re.compile(r"(?<![\w.])(\d{1,4})\s*[~-]\s*(\d{1,4})(?:v\d)?(?![\w.])")
_EP_NUMS = re.compile(r"(?:(?:\b|(?<=\d))S\d{1,2}\s?E\s?(\d{1,4})\b|\b\d{1,2}x(\d{1,4})\b|\bE(?:P|pisode)?\s?(\d{1,4})\b"
                      r"|(?<![\w.])(\d{1,4})(?:v\d)?(?![\w.]))", re.I)
_YEAR = range(1950, 2036)
_YEAR_WORD = re.compile(r"\b(?:19|20)\d\d\b")      # the one spelling of "a year" in this module
# Words subtitle names carry that are neither the show nor another show (measured on the 569 records of
# the 2026-09 corpus): the streaming source, the language, caption flags. Every table language's
# words longer than three letters and its names, not only Arabic's and English's (`Example.Show.Thai.E07.srt`); no
# three-letter code beyond the two measured ones (`may`, `fin`, `cat` are words of titles).
_LANGUAGE_NOISE = sorted({w.lower() for _, words in core.LANGUAGE_TABLE.values() for w in words if len(w) > 3}
                         | {n.lower() for english, own in core.LANGUAGE_NAMES.values() for n in (english, *own)},
                         key=len, reverse=True)
_SUB_NOISE = re.compile(r"\b(?:shahid|osn|amzn|webrip|en|ar|enus|eng|ara|cc|sdh|dub|nf|ass|srt|track\d*|"
                        + "|".join(re.escape(w) for w in _LANGUAGE_NOISE) + r")\b", re.I)


def _spelling(t: str) -> str:
    return str(t or "").replace("×", "x").replace("\u2019", "").replace("'", "").replace("`", "")


def clean_name(name: str) -> str:
    """A subtitle file or release name with its extension, CRC, resolution and codec numbers removed,
    `.`/`_` read as spaces (scene names are dotted, old fansub names underscored), `×` as `x`, and
    apostrophes dropped so "Journey's" and "Journeys" are one word."""
    t = _EXT.sub("", str(name or "").strip())
    t = _HASH.sub(" ", t)
    t = _spelling(t)
    t = _SIZE_VERSION.sub(" ", t)          # before `.` is a space
    t = re.sub(r"[._]+", " ", t)
    t = re.sub(r"(\d{4})(s\d{1,2}e\d)", r"\1 \2", t, flags=re.I)        # "2011s01e07"
    t = _RES.sub(" ", t)
    t = _AUDIO_LAYOUT.sub(r"\1 ", t)       # before `_CODEC_NUM`, whose `flac\d?(?:\s\d)?` leaves the 0 of `FLAC 2 0`
    t = _RELEASE_NUM.sub(" ", t)
    t = _CODEC_NUM.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


_SEASON_WORD = re.compile(r"(?:season|part|cour|\bs)\s*$", re.I)


def _ranges(t: str) -> list:
    """Episode ranges ("01-148", "1~12"); "Season 1 - 02" is a season and an episode, not a range."""
    return [m for m in _RANGE.finditer(t)
            if int(m.group(1)) < int(m.group(2)) and not _SEASON_WORD.search(t[:m.start()])]


def named_episodes(name: str) -> tuple[set[int], list[tuple[int, int]]]:
    """The single episode numbers and the ranges a cleaned name states. A bare year is not an episode."""
    t = clean_name(name)
    ranges = [(int(m.group(1)), int(m.group(2))) for m in _ranges(t)]
    for m in reversed(_ranges(t)):
        t = t[:m.start()] + " " + t[m.end():]
    singles = set()
    for m in _EP_NUMS.finditer(t):
        n = int(next(g for g in m.groups() if g))
        if m.group(4) and n in _YEAR:
            continue
        singles.add(n)
    return singles, ranges


def _show_head(cleaned: str) -> str:
    """The part of a cleaned name before its first episode marker, without the leading `[Group]` tag."""
    cleaned = re.sub(r"^\s*\[[^\]]{1,40}\]\s*", "", cleaned)
    cut = len(cleaned)
    for rx in (_RANGE, _EP_NUMS):
        for m in (_ranges(cleaned) if rx is _RANGE else rx.finditer(cleaned)):
            n = next((g for g in m.groups() if g), "")
            if rx is _EP_NUMS and m.group(4) and int(n) in _YEAR:
                continue
            cut = min(cut, m.start())
            break
    return cleaned[:cut]


def _show_words(cleaned: str) -> str:
    """The show part of a name (`_show_head`) without years and subtitle noise words. Later brackets
    are kept as words: `[PTQ-plumsubs][LanternXLantern][01]` names its show inside one."""
    head = re.sub(rf"\(\s*{_YEAR_WORD.pattern}\s*\)|{_YEAR_WORD.pattern}", " ", _show_head(cleaned))
    head = re.sub(r"[\[\]()]", " ", head)
    return re.sub(r"\s+", " ", _SUB_NOISE.sub(" ", head)).strip(" -")


# A bracket, or parentheses holding more than a lone year: `[BD 2014]`, `(BD 2019)`, `(COMPLETE)` date or
# describe a release. `(1999)` alone is the series' year and stays.
_RELEASE_GROUP = re.compile(rf"\[[^\]]*\]|\((?!\s*{_YEAR_WORD.pattern}\s*\))[^)]*\)")
# Where the release part of a name starts: a source (`BD`, `BluRay`, `WEB`, `DVD`, `Remux`), a pack word
# (`Complete`, `Batch`) or a bare season (`S01`, `Season 2`). A year after one of these, or one right before a
# source (`Show 2014 BD - 04`), dates the release, not the series.
_SOURCE = r"(?:bd|bdrip|bdremux|blu-?ray|remux|dvd|dvdrip|web|web-?dl|webrip)"
_RELEASE_WORD = re.compile(rf"\b(?:{_SOURCE}|complete|batch|s\d{{1,2}}|season\s*\d{{1,2}})\b", re.I)
_YEAR_BEFORE_SOURCE = re.compile(rf"(?<!\()\s*{_YEAR_WORD.pattern}\s*$")


def _strip_release_groups(head: str) -> str:
    return _RELEASE_GROUP.sub(" ", head)


def series_years(cleaned: str) -> set[int]:
    """The years a cleaned name gives its series: a year in the show part, `(1999)`, `.1999.` or ` 1999 -`.
    Not read: a year after the episode marker (an episode title such as "Year 2000"), inside
    `[...]` or another parenthesised group (`[BD 2014]`, `(TV 1999)`), after a release word (`Complete BD
    2014`, `S01 2014`) or right before a source (`2014 BluRay`)."""
    head = _strip_release_groups(_show_head(cleaned))
    m = _RELEASE_WORD.search(head)
    if m:
        before = head[:m.start()]
        head = _YEAR_BEFORE_SOURCE.sub(" ", before) if re.fullmatch(_SOURCE, m.group(0), re.I) else before
    return {int(y) for y in _YEAR_WORD.findall(head) if int(y) in _YEAR}


def _years_text(years) -> str:
    return "/".join(map(str, sorted(years)))


def year_conflict(cleaned: str, target: dict, pairs_from: str = "") -> tuple[str | None, str]:
    """Does this cleaned name date another series? `(None, "")` when it names no year, names one of the
    target's years, or no year is known; `("drop", reason)`; or `("waived", reason)` for a later-season
    entry whose name carries an earlier year *and* states one of the target's S/E pairs, the shape scene
    and library names give every season (`MOSS.x.GARDEN.2022.S02E03` for the 2023 entry). An old fansub's
    `(1981) - 04` states no pair and is dropped. `pairs_from` is another name whose pairs count too (a pack
    file's own name when its release name is checked)."""
    years = target.get("years") or set()
    named = series_years(cleaned) if years else set()
    if not named or named & years:
        return None, ""
    stated = _stated_pairs(cleaned) | _stated_pairs(pairs_from)
    fits = sorted(stated & (target.get("pairs") or set()))
    if target.get("sequel") and max(named) < min(years) and fits:
        return "waived", (f"ok (sequel: series year {_years_text(named)} before {_years_text(years)}, "
                          f"S{fits[0][0]:02d}E{fits[0][1]:02d} fits)")
    return "drop", f"another year: {_years_text(named)} (the show's {_years_text(years)})"


def show_years(mapping: dict, variants: list[str], season_year: int | None = None) -> tuple[set[int], dict]:
    """The years this entry goes by, and which source gave which: AniList's `seasonYear`, the year ani.zip's
    first episode aired (never the played episode's: Lantern x Lantern (2011) ep 100 aired in 2013), and
    any year the entry's own titles carry ("Lantern x Lantern (2011)"). Any of them may date a file."""
    years, sources = set(), {}
    y = _pos_int(season_year)
    if y in _YEAR:
        years.add(y)
        sources["anilist"] = y
    ep1 = ((mapping or {}).get("episodes") or {}).get("1") or {}
    m = _YEAR_WORD.match(str(airdate(ep1) or ""))
    if m and int(m.group(0)) in _YEAR:
        years.add(int(m.group(0)))
        sources["ani.zip"] = int(m.group(0))
    named = sorted({int(t) for v in variants or [] for t in _YEAR_WORD.findall(str(v)) if int(t) in _YEAR})
    if named:
        years |= set(named)
        sources["titles"] = named
    return years, sources


def entry_is_sequel(mapping: dict, title_season: set) -> str | None:
    """Why this entry is a later season, or None. Its own titles name season 2 or more; or ani.zip files its
    episode 1 under season 2 or more, or numbers it past 1 absolutely (a continuation cour TVDB keeps in
    season 1; every sequel of the 2026-09 corpus reads 21-62 there). Never the played episode's season:
    ani.zip files Lantern x Lantern (2011) ep 60 under season 2."""
    if any(n >= 2 for n in title_season):
        return "titles"
    ep1 = ((mapping or {}).get("episodes") or {}).get("1") or {}
    season, absolute = ep1.get("seasonNumber"), _pos_int(ep1.get("absoluteEpisodeNumber"))
    if isinstance(season, int) and not isinstance(season, bool) and season >= 2:
        return "ani.zip season"
    if absolute and absolute > 1:
        return "ani.zip absolute"
    return None


def _compact(s: str) -> str:
    return re.sub(r"[\W_]+", "", clean_name(s).lower())


def _norm_words(s: str) -> set[str]:
    return set(re.sub(r"[\W_]+", " ", clean_name(s).lower()).split())


_SEASON_TOKEN = re.compile(r"\bs\d{1,2}\b|\bseason\s*\d{1,2}\b|\b\d{1,2}(?:st|nd|rd|th)\s+season\b", re.I)


def _glued_title(word: str, variant: str) -> bool:
    """Is this "other show" word just the title written without spaces ("lanternxlantern" for
    "Lantern x Lantern (2011)")? It must start the title and cover most of it: "kazehiki" inside
    "Towakazehiki" is another show (Kazehiki Series Second Season), not a spelling.
    The release show test keeps a franchise stem inside a title's word (`score.spelling_reading`), because it
    reads a name the similarity bar has already passed, where the stem is the franchise's own pack; a subtitle file's
    show word read here has passed nothing, and the stem names another show."""
    w, v = _compact(word), _compact(variant)
    return bool(w and v) and v.startswith(w) and len(w) >= 0.7 * len(v)


def _same_show(show: str, variants: list[str]) -> bool:
    """Series similarity as releases use it, plus two shapes subtitle names take that it under-rates:
    a shortened title ("Pellin" for "Tabiji no Pellin 2nd Season") and a glued one
    ("MossxGardenSeason2")."""
    if series_similarity(show, variants)[0] >= SERIES_SIMILARITY:
        return True
    show = _SEASON_TOKEN.sub(" ", show)
    words, flat = _norm_words(show), _compact(show)
    for v in variants or []:
        if words and words <= _norm_words(v):
            return True
        cv = _compact(v)
        if len(flat) >= 5 and cv and (flat in cv or cv in flat):
            return True
    return False


def fan_code_only(spelling: str, mapping: dict, anilist_titles=()) -> bool:
    """Is this spelling among the entry's variants only because ani.zip's `x-jat` put it there? `x-jat`
    is AniDB's romaji short title; for a franchise it is a fan code whose number counts the franchise's series (`tdl s8`
    for an eighth entry), not the season a group writes, so no season is read from it. The test is by origin, not by
    string: a spelling that is also ani.zip's `en` or one of AniList's own titles (`anilist.known_spellings`) is a
    title, whatever `x-jat` says. With AniList's titles unknown, the string is the code's alone."""
    zt = (mapping or {}).get("titles") or {}
    return bool(spelling) and spelling == zt.get("x-jat") and spelling != zt.get("en") and spelling not in (anilist_titles or ())


def entry_seasons(variants: list[str], mapping: dict, anilist_titles=()) -> list[tuple[int, str]]:
    """The seasons the entry's titles state, each with the title that states it, in `title_variants`'
    order (`Tidewater Lantern Second Season` -> `(2, that title)`); the one source for the resolve's season filter
    (`cli.entry_season`), the entry identity (`cli.entry_identity`) and the subtitle plan's sequel verdict
    (`episode_plan`). A spelling that is only the fan code (`fan_code_only`) is not read: measured on the 2026-09
    corpus, `x-jat`'s number turned the season filter against three entries' own releases. A trailing number
    (`Tidewater Lantern 2`, a synonym's bare `3`) is not a season statement either: measured, reading it would have
    rejected every `S01E11` release of one 2025 entry (`score.season_number` decides what a statement is)."""
    return [(n, v) for v in variants or [] if not fan_code_only(v, mapping, anilist_titles) and (n := season_number(v))]


def title_seasons(variants: list[str], mapping: dict | None = None, anilist_titles=()) -> set[int]:
    """The seasons the entry's own titles name, for the subtitle target's pairs: `entry_seasons`' statements, plus a
    trailing number in a short title ("Towakazehiki 2", "MxG 2").
    The trailing number stays here and not in `entry_seasons` because a pair only admits a subtitle name that states
    it. A spelling that is only ani.zip's fan code is not read (`fan_code_only`)."""
    out = {n for n, _ in entry_seasons(variants, mapping or {}, anilist_titles)}
    for v in variants or []:
        if fan_code_only(v, mapping or {}, anilist_titles):
            continue
        m = re.search(r"[A-Za-z]\s+(\d{1,2})$", v.strip())
        if m and 1 < int(m.group(1)) <= 12:
            out.add(int(m.group(1)))
    return out


def episode_target(episode: int, *, season: int | None = None, season_episode: int | None = None,
                   absolute: int | None = None, title_season: int | set | None = None,
                   years: set | None = None, sequel: bool = False) -> dict:
    """What a file may call this episode. `episode` is AniList's number; `season`/`season_episode` the
    TVDB-style pair ani.zip gives; `absolute` the running number; `title_season` the season AniList's
    own titles name ("Tabiji no Pellin 2nd Season" -> 2; see `title_seasons`).

    Pairs are what a name stating a season must match; `numbers` what a name without one may say.
    The absolute number goes with season 1 or none: Subdl files Pellin S2 ep 2 as "S01E30".

    `years` are the years the entry goes by (`show_years`); empty means no year check. `sequel` says the
    entry is a later season, whose files may carry the series' first year (`MOSS.x.GARDEN.2022.S02E03`
    for a 2023 entry), so an earlier year is accepted for it.
    """
    named = {title_season} if isinstance(title_season, int) else set(title_season or ())
    pairs = {(n, episode) for n in named} or {(1, episode)}
    if season is not None and season_episode:
        pairs.add((season, season_episode))
    numbers = {episode} | {n for n in (season_episode, absolute) if n}
    if absolute:
        pairs.add((1, absolute))
    return {"episode": episode, "pairs": pairs, "numbers": numbers, "years": set(years or ()),
            "sequel": bool(sequel)}


_NAMED_SEASON_EP = re.compile(r"(?:\b|(?<=\d))S(\d{1,2})\s?E\s?(\d{1,4})\b|\b(\d{1,2})x(\d{1,4})\b", re.I)


def _stated_pairs(cleaned: str) -> set[tuple[int, int]]:
    return {(int(m.group(1) or m.group(3)), int(m.group(2) or m.group(4))) for m in _NAMED_SEASON_EP.finditer(cleaned)}


_BARE_NUMBER = re.compile(r"(?<![^\W_])\d{1,4}(?![^\W_])")


def _set_aside_title_numbers(cleaned: str, variants: list[str]) -> tuple[str, set[int]]:
    """A movie's cleaned name without the numbers its own titles carry (`… Movie 1 - …` gives 1), taken out
    only where the name spells a title as the title spells it: its words in order, with a dash between two words
    only where the title has one, so `Title - 2` and `Title 2 - 02` still name episode 2 for the title `Title 2`.
    Only the number goes: the words after it stay in the show part, so `Title 2 Other Story` is still read for
    another show. Returns the name and the numbers taken out."""
    spans = []
    for v in variants or []:
        numbers = named_episodes(v)[0]
        parts = re.split(r"([^\W_]+)", clean_name(v))
        words, gaps = parts[1::2], parts[2:-1:2]
        if not numbers or not words:
            continue
        rx = re.escape(words[0]) + "".join((r"[\W_]+" if "-" in g else r"[^\w-]+") + re.escape(w)
                                           for g, w in zip(gaps, words[1:]))
        spans += [(m.start(), m.end(), numbers) for m in re.finditer(rf"(?<![^\W_]){rx}(?![^\W_])", cleaned, re.I)]
    found = set()

    def blank(m):
        n = int(m.group())
        if any(a <= m.start() and m.end() <= b and n in nums for a, b, nums in spans):
            found.add(n)
            return " "
        return m.group()
    return (_BARE_NUMBER.sub(blank, cleaned) if spans else cleaned), found


# Words a movie's file names carry after its title that name no other show.
_MOVIE_WORDS = re.compile(r"\b(?:movie|film|gekijouban|eiga)\b", re.I)
_OPEN_BRACKET = re.compile(r"[\[(]")


def _movie_title_part(cleaned: str, title_years: set[str] = frozenset()) -> str:
    """The title part of a movie's cleaned file name: the show part (`_show_head`) up to its first year,
    bracket or parenthesis, or release word (a source, `Complete`, `Batch`, `S01`), without subtitle noise and
    movie words. Scene and fansub names put the year, the release and the group after the title
    (`Title.2003.1080p.BluRay.x264-GRP`, `[Grp] Title - Movie [1080p]`). A year the movie's own titles carry
    (`title_years`, "Title 1999") is part of the title, not its end."""
    head = _show_head(cleaned)
    cut = len(head)
    for m in _YEAR_WORD.finditer(head):
        if m.group(0) not in title_years:
            cut = m.start()
            break
    for rx in (_OPEN_BRACKET, _RELEASE_WORD):
        m = rx.search(head)
        if m:
            cut = min(cut, m.start())
    return re.sub(r"\s+", " ", _MOVIE_WORDS.sub(" ", _SUB_NOISE.sub(" ", head[:cut]))).strip(" -|")


def _other_show(cleaned: str, show: str, variants: list[str], movie: bool) -> tuple[str | None, str | None]:
    """The other show this name names (`sibling_name`), and for a movie the words the whole-name
    reading would have named that the title-part reading set aside, else None.

    A movie's file name has no episode marker, so the whole show part runs on into its group, source and
    format words ("QORVX", "VOSTFR", "Movie"). For a movie only the title part is read (measured on
    2026-09-29). A title part that does not match the movie on its own
    (`Other Film (The Movie) [BD]`) is read whole, as before."""
    other = sibling_name(show, variants)
    if other and any(_glued_title(other, v) for v in variants):
        other = None
    if not movie or not other:
        return other, None
    part = _movie_title_part(cleaned, {y for v in variants for y in _YEAR_WORD.findall(v)})
    whole = _MOVIE_WORDS.sub(" ", show)
    # The movie words are set aside in the titles too, or a short "Title Movie" no longer matches a name read
    # without its "Movie", and the sibling test reads nothing.
    variants = [t for t in (re.sub(r"\s+", " ", _MOVIE_WORDS.sub(" ", v)).strip() for v in variants) if t]
    if re.search(r"[^\W\d_]{2,}", part) and _same_show(part, variants):
        reading = sibling_name(part, variants)
    else:
        reading = sibling_name(whole, variants)
    if reading and any(_glued_title(reading, v) for v in variants):
        reading = None
    return reading, (other if not reading else None)


def name_check(name: str, variants: list[str], target: dict, one_episode_record: bool) -> tuple[bool, str]:
    """Does this file name fit the show and episode? `(keep, reason)`; a failed check drops, never warns.

    `target` is `episode_target(...)`. A name that states a range fits when the range holds one of the
    target's numbers; such a file is a pack, not a one-episode record. A name that states a season
    must state one of the target's (season, episode) pairs.
    """
    wanted = target["numbers"]
    # The titles get the same apostrophe and `×` treatment as the name, or "Journey’s" is two words
    # on one side and one on the other.
    variants = [_spelling(v) for v in variants or []]
    cleaned = clean_name(name)
    title_numbers = set()
    if target.get("movie"):
        # A movie's target has no episode number, so any number read drops the file. A number the
        # movie's own title carries where the name spells that title (`Movie 1`) is the movie itself.
        cleaned, title_numbers = _set_aside_title_numbers(cleaned, variants)
    singles, ranges = named_episodes(cleaned)
    if target.get("movie"):
        # So is the ani.zip pair that files the movie as a special (`S00E10`).
        own_pairs = sorted(p for p in _stated_pairs(cleaned) if p in target["pairs"])
        singles -= {e for _, e in own_pairs}
        own = ", ".join([f"title number {n}" for n in sorted(title_numbers)]
                        + [f"ani.zip pair S{s:02d}E{e:02d}" for s, e in own_pairs])
    else:
        own = ""
    in_range = any(a <= w <= b for a, b in ranges for w in wanted)
    if singles and not singles & wanted and not in_range:
        return False, f"names episode {sorted(singles)[:3]}"
    if ranges and not in_range and not singles & wanted:
        return False, f"names episodes {ranges[0][0]}-{ranges[0][1]}"
    for m in _NAMED_SEASON_EP.finditer(cleaned):
        se = (int(m.group(1) or m.group(3)), int(m.group(2) or m.group(4)))
        if se not in target["pairs"]:
            return False, f"names S{se[0]:02d}E{se[1]:02d}"
    show = _show_words(cleaned)
    if not re.search(r"[^\W\d_]{2,}", show):
        return (True, "numbering only") if one_episode_record else (False, "names no show and is not one episode")
    stated_season = season_number(cleaned)
    if stated_season is not None:
        if stated_season not in {s for s, _ in target["pairs"]}:
            return False, f"names season {stated_season}"
        fits = (singles & wanted) | {w for w in wanted for a, b in ranges if a <= w <= b}
        if fits and not any((stated_season, e) in target["pairs"] for e in fits):
            return False, f"names season {stated_season} episode {sorted(fits)[0]}"
    if not _same_show(show, variants):
        return False, f"series {series_similarity(show, variants)[0]:.2f}"
    # "Lantern x Lantern (1999) - 04" names the same words as the 2011 show but another series.
    year_verdict, year_why = year_conflict(cleaned, target)
    if year_verdict == "drop":
        return False, year_why
    other, set_aside = _other_show(cleaned, show, variants, bool(target.get("movie")))
    if other:
        return False, f"another show: {other}"
    if year_verdict == "waived":
        return True, year_why
    if set_aside:
        return True, f"ok (movie: set aside {set_aside}" + (f"; {own})" if own else ")")
    if own:
        return True, f"ok (movie: {own})"
    return True, "pack" if in_range and not singles & wanted else "ok"


def release_name_check(release_name: str, file_name: str, target: dict) -> tuple[bool, str]:
    """A Subdl pack's release name may date the series where its files do not: `[Grp] Lantern x Lantern
    (1999) (COMPLETE)` holding `..._-_004_....srt`. Only the year is read here; the file's
    own name goes through `name_check`. The file's S/E pairs count for the sequel waiver."""
    if not release_name or release_name == file_name:
        return True, ""
    verdict, why = year_conflict(clean_name(release_name), target, pairs_from=clean_name(file_name))
    if verdict == "drop":
        return False, why.replace("another year:", "another year in the release name:", 1)
    return True, why


# ----------------------------------------------------------------------------- ids and the episode plan

def _imdb(value) -> str | None:
    v = str(value or "").strip()
    return v if re.fullmatch(r"tt\d{5,10}", v) else None


def _imdb_number(value) -> int | None:
    """An IMDb id as its number: `tt0400201` (ani.zip, Subdl) and 400201 (OpenSubtitles) are one id; "", 0 and junk
    are none (Subdl sends `imdb_id: ""` for a show it has no IMDb id for)."""
    if isinstance(value, bool):
        return None
    m = re.fullmatch(r"(?:tt)?(\d{1,10})", str(value or "").strip())
    if not m:
        return None
    return int(m.group(1)) or None


# ----------------------------------------------------------------------------- the show a title-step record names
#
# A title search matches words, not shows: the title step for a one-episode entry answered three
# live-action series whose file names all pass `name_check`, and the first was selected. A title-step record that
# names its show's ids is compared with the entry's (`show_tie`); one that names none is never selected for the viewer.

def os_show_ids(rec: dict) -> dict:
    """The show an OpenSubtitles record is filed under, from `attributes.feature_details`: an episode's
    `parent_imdb_id`, `parent_tmdb_id` and `parent_title` (its own `imdb_id`/`tmdb_id` are the episode's), any other
    feature's (a movie, a show) own `imdb_id`, `tmdb_id` and `title`."""
    fd = ((rec or {}).get("attributes") or {}).get("feature_details") or {}
    pre = "parent_" if str(fd.get("feature_type") or "").lower() == "episode" else ""
    return {"imdb": _imdb_number(fd.get(f"{pre}imdb_id")), "tmdb": _pos_int(fd.get(f"{pre}tmdb_id")),
            "title": fd.get(f"{pre}title"), "type": fd.get("feature_type")}


def subdl_show_ids(results) -> dict:
    """The show a Subdl answer is for: its `results[]` when that names exactly one show (every recorded title answer
    names one or none), else no ids, since no file can be tied to one of several."""
    shows = [r for r in results or [] if isinstance(r, dict)]
    if len(shows) != 1:
        return {"imdb": None, "tmdb": None, "title": None, "type": None, "shows": len(shows)}
    s = shows[0]
    return {"imdb": _imdb_number(s.get("imdb_id")), "tmdb": _pos_int(s.get("tmdb_id")), "title": s.get("name"),
            "type": s.get("type"), "shows": 1}


def show_tie(plan: dict, ids: dict) -> tuple[str, str]:
    """Is this title-step record's show the entry? `("imdb"|"tmdb", why)`: tied by that id; `("other", why)`: another
    show, dropped; `("none", why)`: nothing ties it, so it may be offered but is never selected.

    IMDb decides when both sides have one. A different IMDb id is another show, except for a sequel: ani.zip gives
    some sequels their franchise's id (a `Hatepororogatari Second Season` entry carries the `Pororogatari` id while
    OpenSubtitles files its episodes under the sequel's own), so a sequel's is only not tied. TMDB ties but never
    drops: measured, ani.zip's TMDB id for one show was not OpenSubtitles' while their IMDb ids agreed. TMDB numbers
    are per type (a movie and a show may share one), so they tie only when the record's type is the entry's."""
    ours, theirs = _imdb_number(plan.get("imdb")), ids.get("imdb")
    if ours and theirs:
        if ours == theirs:
            return "imdb", f"the record's show is tt{theirs:07d}, the entry's"
        if plan.get("sequel"):
            return "none", (f"the record's show is tt{theirs:07d}, the entry's tt{ours:07d}: a sequel, whose ani.zip "
                            f"id may be the first series'")
        return "other", f"another show's id: tt{theirs:07d} (the entry's tt{ours:07d})"
    ours_t, theirs_t = _pos_int(plan.get("tmdb")), ids.get("tmdb")
    if ours_t and theirs_t:
        kind, want = _record_kind(ids.get("type")), "movie" if plan.get("movie") else "tv"
        if kind != want:
            word = {"movie": "movie", "tv": "show"}
            return "none", (f"the record's TMDB {theirs_t} comes with no type, and TMDB ids are per type" if not kind
                            else f"the record's TMDB {theirs_t} is a {word[kind]}'s, the entry is a {word[want]}: "
                                 f"TMDB ids are per type")
        if ours_t == theirs_t:
            return "tmdb", f"the record's show is TMDB {theirs_t}, the entry's"
        return "none", f"the record's show is TMDB {theirs_t}, the entry's {ours_t}; TMDB ids alone do not drop"
    if not (ours or ours_t):
        return "none", "the entry has no IMDb or TMDB id"
    shows = ids.get("shows")
    if shows is not None and shows != 1:
        return "none", (f"the answer names {shows} shows, so no file is tied to one" if shows
                        else "the answer names no show")
    return "none", "the record names no show id"


def _record_kind(feature_type) -> str | None:
    """A record's type as "movie" or "tv", from OpenSubtitles' `Movie`, `Episode`, `Tvshow`; Subdl's `movie`, `tv`."""
    t = str(feature_type or "").lower()
    return "movie" if t == "movie" else "tv" if t in ("episode", "tvshow", "tv") else None


def untied(c: dict) -> bool:
    """A candidate nothing ties to the entry (`show_tie` "none"): offered, never selected."""
    return c.get("show_tie") == "none"


def checked_release_ids(mapping: dict, release_ids: dict | None, *, source: str = "indexer",
                        log: Log | None = None) -> dict:
    """The playing release's show ids when they may lead the plan, else `{}`.

    AniDB is the one id both the release and ani.zip carry. When both have it and they differ, the release
    was linked to another series, so none of its ids is used and the plan is ani.zip's alone. When
    either side lacks an AniDB id nothing can be checked and the release's ids are trusted as before. One
    `extsubs.plan anidb_check` line says which it was, with both ids.
    """
    release_ids = release_ids or {}
    if not release_ids:
        return {}
    ours = _pos_int(release_ids.get("anidb"))
    theirs = _pos_int(((mapping or {}).get("mappings") or {}).get("anidb_id"))
    shown = {k: v for k, v in release_ids.items() if v is not None}
    if ours and theirs and ours != theirs:
        if log:
            log.warn("extsubs.plan", "anidb_check", result="DISTRUST", source=source, release_anidb=ours,
                     anizip_anidb=theirs, dropped=shown,
                     reason=f"{source}'s AniDB id {ours} is not ani.zip's {theirs}: the release names another "
                            f"series, so none of its ids is used",
                     suggest=f"the plan uses ani.zip's ids alone; compare https://anidb.net/anime/{ours} and "
                             f"https://anidb.net/anime/{theirs} if the plan looks wrong")
        return {}
    if log:
        if ours and theirs:
            log.info("extsubs.plan", "anidb_check", result="PASS", source=source, release_anidb=ours,
                     anizip_anidb=theirs, ids=shown)
        else:
            lacking = [name for name, aid in ((source, ours), ("ani.zip", theirs)) if not aid]
            log.info("extsubs.plan", "anidb_check", result="SKIP", source=source, release_anidb=ours,
                     anizip_anidb=theirs, ids=shown,
                     reason=f"{' and '.join(lacking)} {'have' if len(lacking) > 1 else 'has'} no AniDB id to "
                            f"compare: the release's ids are trusted unchecked")
    return release_ids


def episode_plan(mapping: dict, episode: int, variants: list[str], *, release_ids: dict | None = None,
                 ids_source: str = "", anilist_format: str = "", season_year: int | None = None,
                 log: Log | None = None, anilist_titles=()) -> dict:
    """What to ask the databases for one episode, and what a file may call it.

    Ids: the playing release's own show ids first (`release_ids`, from its indexer add-on's `show_ids`;
    `ids_source` is that indexer's name, the `ids_from`/`numbering` label) unless its AniDB id
    disagrees with ani.zip's (`checked_release_ids`), then ani.zip's `mappings`. Numbering: ani.zip's
    `episodes[n]` (TVDB-style season/episode and the absolute number), then the release's `tvdb_season`. The
    two id databases disagree on seasons for some shows (LxL, Kazehiki, Pellin S2), which is why every answer
    still goes through `name_check`. `release_ids` has already been checked by `indexers.normalize_show_ids`;
    tmdb, imdb and tvdb_season are re-checked here for direct callers. Years (`show_years`): AniList's
    `season_year` when the caller has it, ani.zip's first airdate and the titles always, so a file dated
    for another series is dropped on every path. Movie: ani.zip's `mappings.type` or
    AniList's `anilist_format`; either one saying MOVIE plans the movie search, so neither source can veto
    it. `format_from` keeps both raw values, `movie_from` names the sources that said MOVIE, and a
    disagreement (one says MOVIE, the other names another format) is logged as a warning.
    """
    source = ids_source or "indexer"
    release_ids = checked_release_ids(mapping, release_ids, source=source, log=log)
    m = (mapping or {}).get("mappings") or {}
    ep = ((mapping or {}).get("episodes") or {}).get(str(episode)) or {}
    tmdb, tmdb_from = (release_ids.get("tmdb"), source) if _pos_int(release_ids.get("tmdb")) else \
        (_pos_int(m.get("themoviedb_id")), "ani.zip")
    imdb, imdb_from = (release_ids.get("imdb"), source) if _imdb(release_ids.get("imdb")) else \
        (_imdb(m.get("imdb_id")), "ani.zip")
    format_from, movie_from = movie_sources(mapping, anilist_format, log, "extsubs.plan", episode=episode)
    movie = bool(movie_from)
    season = ep.get("seasonNumber") if isinstance(ep.get("seasonNumber"), int) else None
    season_episode = _pos_int(ep.get("episodeNumber"))
    numbering = "ani.zip"
    tvdb_season = release_ids.get("tvdb_season")
    if season is None and isinstance(tvdb_season, int) and not isinstance(tvdb_season, bool) and tvdb_season >= 0:
        season, season_episode, numbering = tvdb_season, episode, source
    absolute = _pos_int(ep.get("absoluteEpisodeNumber"))
    # The pairs read the trailing number too; the sequel verdict reads what the resolve reads.
    title_season = title_seasons(variants, mapping, anilist_titles)
    years, years_from = show_years(mapping, variants, season_year)
    sequel_from = entry_is_sequel(mapping, {n for n, _ in entry_seasons(variants, mapping, anilist_titles)})
    sequel = bool(sequel_from)
    # ani.zip files some movies as a TVDB special (`S00E10`); a name stating that pair is the movie.
    # `anizip.movie_pairs` is the one rule (the resolve's release filter reads it too).
    movie_pairs = anizip_movie_pairs(mapping, episode)
    target = {"movie": True, "episode": episode, "pairs": movie_pairs, "numbers": set(), "years": years,
              "sequel": False} if movie else \
        episode_target(episode, season=season, season_episode=season_episode, absolute=absolute,
                       title_season=title_season, years=years, sequel=sequel)
    return {"episode": episode, "movie": movie, "movie_from": movie_from, "format_from": format_from, "tmdb": _pos_int(tmdb), "imdb": imdb,
            "ids_from": {"tmdb": tmdb_from if tmdb else None, "imdb": imdb_from if imdb else None},
            "season": season, "season_episode": season_episode, "absolute": absolute, "numbering": numbering,
            "years": sorted(years), "years_from": years_from, "sequel": target["sequel"],
            "sequel_from": sequel_from if target["sequel"] else None,
            "title": (variants or [""])[0], "variants": list(variants or []), "target": target}


def ladder(plan: dict) -> list[tuple[str, dict]]:
    """The id queries, in order: season/episode, then the absolute number. The title query is not
    here: it runs only when these find nothing that passes the name check."""
    if plan["movie"]:
        return [("id", {})]
    steps = []
    if plan["season"] is not None and plan["season_episode"]:
        steps.append(("s+e", {"season_number": plan["season"], "episode_number": plan["season_episode"]}))
    absolute = plan["absolute"]
    if absolute and (absolute != plan["season_episode"] or plan["season"] not in (1, None)):
        steps.append(("abs", {"episode_number": absolute}))
    if not steps:
        steps.append(("s+e", {"season_number": 1, "episode_number": plan["episode"]}))
    return steps


# ----------------------------------------------------------------------------- the services

class ServiceSkipped(Exception):
    """This service is out for the rest of the search: a 429, a rejected key, no key."""


_next_slot: dict[str, float] = {}
_pace_lock = threading.Lock()


def _pace(service: str) -> float:
    """Wait for this service's next free slot; returns the seconds waited. The slot is reserved under the
    lock and slept for outside it, so one service's spacing never holds up the other."""
    with _pace_lock:
        now = time.monotonic()
        slot = max(now, _next_slot.get(service, 0.0))
        _next_slot[service] = slot + PACING_S[service]
    wait = slot - now
    if wait > 0:
        time.sleep(wait)
    return wait


class _Client:
    service = ""

    def __init__(self, http: Http, log: Log, token: str):
        self.http, self.log, self.token = http, log, token

    def _get(self, stage: str, url: str, headers: dict | None = None, log_url: str | None = None):
        waited = _pace(self.service)
        if waited:
            self.log.debug(f"extsubs.{self.service}", "pace", waited_s=round(waited, 3))
        # One attempt: a 429 is answered by skipping the service, never by looping.
        status, body, hdrs = self.http.request(f"extsubs.{self.service}.{stage}", "GET", url, headers=headers,
                                               retries=1, max_retry_after=0, log_url=log_url)
        if status == 429:
            lower = {k.lower(): v for k, v in (hdrs or {}).items()}
            raise ServiceSkipped(f"429 (retry-after {lower.get('retry-after') or '?'})")
        if status in (401, 403):
            raise ServiceSkipped(f"the key was refused ({status})")
        return status, body, hdrs


class SubdlClient(_Client):
    """Subdl v1 search. The key rides in the query string (Subdl's design); the log line shows the URL
    with it masked, and the log sink masks every stored token besides."""
    service = "subdl"

    def search(self, plan: dict, lang: str, params: dict, stage: str) -> list[dict]:
        q = {"type": "movie" if plan["movie"] else "tv", "languages": service_codes(lang, self.service),
             "subs_per_page": 30, "unpack": 1, **params}
        if "film_name" not in q:
            if plan["tmdb"]:
                q["tmdb_id"] = str(plan["tmdb"])
            elif plan["imdb"]:
                q["imdb_id"] = plan["imdb"]
            else:
                return []
        out = []
        for page in range(1, MAX_PAGES + 1):
            query = urllib.parse.urlencode({**q, "page": page})
            status, body, _ = self._get(stage, f"{SUBDL_SEARCH}?api_key={self.token}&{query}",
                                        log_url=f"{SUBDL_SEARCH}?api_key=<redacted>&{query}")
            if status != 200 or not isinstance(body, dict) or body.get("status") is False:
                self.log.info("extsubs.subdl", stage, result="EMPTY", status=status,
                              error=(body or {}).get("error") if isinstance(body, dict) else None)
                break
            # The answer names its show once, in `results[]`; each file carries it for the title step's check.
            show = subdl_show_ids(body.get("results"))
            out += [{**s, "_show": show} for s in body.get("subtitles") or [] if isinstance(s, dict)]
            if page >= int(body.get("totalPages") or 1):
                break
        return out


class OpenSubtitlesClient(_Client):
    service = "opensubtitles"

    def search(self, plan: dict, lang: str, params: dict, stage: str) -> list[dict]:
        q = {"languages": service_codes(lang, self.service), **params}
        if "query" not in q:
            prefix = "" if plan["movie"] else "parent_"
            if plan["tmdb"]:
                q[f"{prefix}tmdb_id"] = plan["tmdb"]
            elif plan["imdb"]:
                q[f"{prefix}imdb_id"] = int(plan["imdb"][2:])
            else:
                return []
        out = []
        for page in range(1, MAX_PAGES + 1):
            # OpenSubtitles' best practice: parameters sorted and lower case, or it redirects.
            query = urllib.parse.urlencode(sorted({**q, **({"page": page} if page > 1 else {})}.items()))
            status, body, _ = self._get(stage, f"{OS_API}/subtitles?{query}", headers={"Api-Key": self.token})
            if status != 200 or not isinstance(body, dict):
                self.log.info("extsubs.opensubtitles", stage, result="EMPTY", status=status)
                break
            out += body.get("data") or []
            if page >= int(body.get("total_pages") or 1):
                break
        return out


CLIENTS = {"subdl": SubdlClient, "opensubtitles": OpenSubtitlesClient}


# ----------------------------------------------------------------------------- records -> candidates

def _fmt(name: str, declared: str = "") -> str:
    f = (declared or "").lower() or (name.rsplit(".", 1)[-1].lower() if "." in name else "")
    return f if f in ("srt", "ass", "ssa", "vtt", "sub") else ""


def _subdl_file_url(path: str) -> str:
    """Subdl's download link with its `api_key=` removed: measured, a keyless download does not
    count against the key's 50/day, and the key then never reaches a URL we store."""
    parts = urllib.parse.urlsplit(path or "")
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k.lower() != "api_key"]
    tail = parts.path + (("?" + urllib.parse.urlencode(query)) if query else "")
    return tail if tail.startswith("http") else SUBDL_DL + tail


def _log_year(log: Log | None, service: str, step: str, name: str, why: str, release: str = "") -> None:
    """One line per year decision: the file, the years its name gives the series, the show's."""
    if not log or not (why.startswith("another year") or why.startswith("ok (sequel")):
        return
    dated = release if "release name" in why else name
    log.info(f"extsubs.{service}", "year", result="DROP" if why.startswith("another year") else "WAIVED",
             step=step, name=name, release=release or None, named_years=sorted(series_years(clean_name(dated))),
             reason=why)


# How many dropped names one search step lists in its `names` line. The 2026-09 corpus drops at most 60
# names in one step (an episode's pack answer); a movie's answer drops a handful.
DROPPED_NAMES_MAX = 100
DROPPED_NAME_CHARS = 200          # each logged name or release name is cut to this; service text, not ours


def _cut(text: str) -> str:
    return text if len(text) <= DROPPED_NAME_CHARS else text[:DROPPED_NAME_CHARS - 1] + "…"


def _log_dropped(log: Log | None, service: str, step: str, drops: list[tuple[str, str, str]]) -> None:
    """The names this step dropped after reading them, each with its reason and, for a Subdl pack
    file, the pack's release name. A drop decided by the record's language or episode field is only counted."""
    if not log or not drops:
        return
    log.info(f"extsubs.{service}", "names", result="DROP", step=step, count=len(drops),
             names=[{"name": _cut(n), "reason": why, **({"release": _cut(r)} if r and r != n else {})}
                    for n, why, r in drops[:DROPPED_NAMES_MAX]],
             more=max(0, len(drops) - DROPPED_NAMES_MAX))


def _record_tie(log: Log | None, service: str, step: str, plan: dict, name: str, ids: dict) -> dict:
    """The candidate's `show_tie`, `show_tie_why` and `show_ids` (the record's) for a title-step record whose name
    passed, with one `extsubs.<service> show_id` line naming both sides' ids; an id step's record is "query": the
    database was asked by the entry's own id. The candidate keeps the reason and ids, so a cached answer's play can say
    why a file is offered without searching again."""
    if step != "title":
        return {"show_tie": "query", "show_tie_why": None, "show_ids": None}
    tie, why = show_tie(plan, ids)
    record = {k: ids.get(k) for k in ("imdb", "tmdb", "type", "shows")}
    if log:
        log.info(f"extsubs.{service}", "show_id", result={"other": "DROP", "none": "UNTIED"}.get(tie, "PASS"),
                 step=step, name=_cut(name), tie=tie, record=record,
                 record_title=_cut(str(ids.get("title") or "")) or None,
                 entry={"imdb": _imdb_number(plan.get("imdb")), "tmdb": _pos_int(plan.get("tmdb")),
                        "sequel": plan.get("sequel")},
                 reason=why, suggest="offered for the viewer to choose, never selected" if tie == "none" else None)
    return {"show_tie": tie, "show_tie_why": why, "show_ids": record}


def subdl_candidates(records: list[dict], plan: dict, lang: str, step: str,
                     log: Log | None = None) -> tuple[list[dict], Counter]:
    """One candidate per file that fits. A pack answers with its file list (`unpack=1`); a file
    whose own episode is 0 (common: the `[PTQ-plumsubs]` pack lists every file as S00E00) has only
    its name to say which episode it is, so the name must say it."""
    want = service_codes(lang, "subdl").split(",")
    target, variants = plan["target"], plan["variants"]
    kept, dropped, names = [], Counter(), []
    for rec in records:
        code = (rec.get("language") or "").upper()
        if code not in want:
            dropped["language"] += 1
            continue
        files = rec.get("unpack_files") or []
        pack = bool(files) and len(files) > 1 or bool(rec.get("full_season")) or \
            (rec.get("episode_from") or 0) != (rec.get("episode_end") or 0)
        entries = files or [None]
        for f in entries:
            name = (f or {}).get("name") or rec.get("release_name") or rec.get("name") or ""
            file_ep = (f or {}).get("episode") or (None if pack else rec.get("episode"))
            one = bool(f) and bool(file_ep) or not pack
            if not target.get("movie") and file_ep and file_ep not in target["numbers"]:
                dropped["their episode number"] += 1
                continue
            ok, why = name_check(name, variants, target, one)
            release = rec.get("release_name") or ""
            if ok and f is not None:
                rok, rwhy = release_name_check(release, name, target)
                if not rok or rwhy:
                    ok, why = rok, rwhy
            _log_year(log, "subdl", step, name, why, release)
            if ok and not target.get("movie") and not file_ep and not named_episodes(name)[0] & target["numbers"]:
                ok, why = False, "only a pack says which episode"
            if ok and f is None and pack:
                ok, why = False, "a pack without a file list"
            tied = {}
            if ok:
                tied = _record_tie(log, "subdl", step, plan, name, rec.get("_show") or {})
                if tied["show_tie"] == "other":
                    ok, why = False, tied["show_tie_why"]
            if not ok:
                dropped[why.split(":")[0].split(" [")[0]] += 1
                names.append((name, why, release))
                continue
            url = (f or {}).get("url") or rec.get("url") or ""
            file_id = re.sub(r"[^A-Za-z0-9_-]+", "-", urllib.parse.urlsplit(url).path.strip("/").removeprefix("subtitle/"))
            kept.append({"service": "subdl", "file_id": file_id, "name": name,
                         "release": rec.get("release_name") or "", "uploader": rec.get("author") or "",
                         "downloads": None, "hearing_impaired": bool((f or {}).get("hi", rec.get("hi"))),
                         "machine": False, "format": _fmt(name, (f or {}).get("format")),
                         "season": (f or {}).get("season") or rec.get("season"), "episode": file_ep,
                         "url": _subdl_file_url(url), "zip": f is None, "pack": pack, "check": why, "step": step,
                         "md5": (f or {}).get("md5") or None, "code": code,
                         "region_rank": region_rank(lang, "subdl", code), **tied})
    _log_dropped(log, "subdl", step, names)
    return kept, dropped


def os_candidates(records: list[dict], plan: dict, lang: str, step: str,
                  log: Log | None = None) -> tuple[list[dict], Counter]:
    want = service_codes(lang, "opensubtitles").split(",")
    target, variants = plan["target"], plan["variants"]
    kept, dropped, names = [], Counter(), []
    for rec in records:
        a = rec.get("attributes") or {}
        code = (a.get("language") or "").lower()
        if code not in want:
            dropped["language"] += 1
            continue
        files = a.get("files") or []
        if len(files) != 1:
            dropped["not one file"] += 1
            continue
        fd = a.get("feature_details") or {}
        se, ep = fd.get("season_number"), fd.get("episode_number")
        if not target.get("movie") and (se, ep) not in target["pairs"] and ep not in target["numbers"]:
            dropped["their episode number"] += 1
            continue
        name = a.get("release") or files[0].get("file_name") or ""
        ok, why = name_check(name, variants, target, True)
        _log_year(log, "opensubtitles", step, name, why)
        tied = {}
        if ok:
            tied = _record_tie(log, "opensubtitles", step, plan, name, os_show_ids(rec))
            if tied["show_tie"] == "other":
                ok, why = False, tied["show_tie_why"]
        if not ok:
            dropped[why.split(":")[0].split(" [")[0]] += 1
            names.append((name, why, ""))
            continue
        kept.append({"service": "opensubtitles", "file_id": str(files[0].get("file_id")), "name": name,
                     "release": a.get("release") or "", "uploader": (a.get("uploader") or {}).get("name") or "",
                     "downloads": a.get("download_count"), "hearing_impaired": bool(a.get("hearing_impaired")),
                     "machine": bool(a.get("ai_translated") or a.get("machine_translated")),
                     "trusted": bool(a.get("from_trusted")), "format": "srt",
                     "season": se, "episode": ep, "url": None, "zip": False, "pack": False, "check": why,
                     "step": step, "code": code, "region_rank": region_rank(lang, "opensubtitles", code),
                     **tied})
    _log_dropped(log, "opensubtitles", step, names)
    return kept, dropped


NORMALIZE = {"subdl": subdl_candidates, "opensubtitles": os_candidates}


# ----------------------------------------------------------------------------- ranking and dedupe

def affinity(cand: dict, playing_title: str) -> tuple[int, list[str]]:
    """How closely a file's names match the playing release: its group (2), its streaming source (1),
    its episode tag style (1). `[PTQ] Lantern X Lantern (2011) - [001 ~ 148][Vellum BD]` names Vellum."""
    if not playing_title:
        return 0, []
    p = parse_title(playing_title)
    text = f"{cand.get('release') or ''} {cand.get('name') or ''}"
    low = text.lower()
    score, why = 0, []
    group = p.get("group_key") or ""
    if len(group) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(group)}(?![a-z0-9])", low):
        score, why = score + 2, why + [f"group {p['group']}"]
    src = streaming_source(p) or p.get("source") or ""
    csrc = parse_title(text).get("source") or ""
    if src and csrc and src == csrc:
        score, why = score + 1, why + [f"source {src}"]
    tag = re.search(r"S\d{2}E\d{2,3}|-\s\d{2,3}\b", playing_title)
    if tag:
        shape = re.sub(r"\d", "0", tag.group(0))
        if re.search(re.escape(shape).replace("0", r"\d"), text):
            score, why = score + 1, why + ["episode tag"]
    return score, why


def rank_key(c: dict) -> tuple:
    """Best first: a file tied to the entry's show (`untied` ones last of all, so the first track is one that may be
    selected whenever any is), human translation (machine-translated last), the viewer's own variant (`region_rank`:
    its own code, the language's, another variant's), verified timing, affinity with the playing release, downloads,
    then hearing-impaired below. Last tie-break: a file whose name names the show over a bare `4.srt`."""
    return (not untied(c), not c.get("machine"), -c.get("region_rank", 0), c.get("timing_verified") is True,
            c.get("affinity", 0), c.get("downloads") or 0, not c.get("hearing_impaired"),
            c.get("check") != "numbering only")


def rank(cands: list[dict], playing_title: str = "") -> list[dict]:
    for c in cands:
        c["affinity"], c["affinity_why"] = affinity(c, playing_title)
    return sorted(cands, key=rank_key, reverse=True)


def dedupe_exact(ranked: list[dict]) -> tuple[list[dict], int]:
    """Drop later copies of a file Subdl lists with the same md5 (measured: LxL ep 4's Vellum file sits in
    two packs, the Netflix file in three, `4.srt` in four). The best-ranked copy, the one whose name
    matched the playing release best, stays. Before any download."""
    seen, out, dropped = set(), [], 0
    for c in ranked:
        key = (c["service"], c.get("md5")) if c.get("md5") else None
        if key and key in seen:
            dropped += 1
            continue
        if key:
            seen.add(key)
        out.append(c)
    return out, dropped


DUP_COUNT_RATIO = 0.02
DUP_MATCH_RATIO = 0.90


def same_file(a: list[float], b: list[float], log: Log | None = None) -> bool:
    """As measured on the eight real LxL ep 4 files: two files are one when their cue counts are
    within 2 % and at least 90 % of one's cues land on the other's after aligning (the offset
    estimator, 180 s window). The duplicates score 1.00 (the Netflix file on both services) and 0.98
    (`[PTQ-plumsubs]` and the Vellum-timed `[PTQ]`, +80.15 s); the closest two *different* translations
    score 0.71. The plan's "first 20 cues" missed the PTQ pair: its opening seven cues stay put while
    the body moves 80 s, so a file's head is the worst place to compare."""
    if not a or not b or abs(len(a) - len(b)) > DUP_COUNT_RATIO * max(len(a), len(b)):
        return False
    _, ratio = estimate_offset(a, b, EXTERNAL_MAX_OFFSET_S, log=log)
    return ratio >= DUP_MATCH_RATIO


def dedupe(cands: list[dict]) -> tuple[list[dict], list[tuple[dict, dict]]]:
    """Keep one of each group of `same_file` candidates (by `cues`, sorted cue starts), the one with more
    downloads. Returns `(kept, [(dropped, kept_instead)])`; order is preserved otherwise."""
    kept: list[dict] = []
    gone: list[tuple[dict, dict]] = []
    for c in sorted(cands, key=lambda c: -(c.get("downloads") or 0)):
        twin = next((k for k in kept if same_file(c.get("cues") or [], k.get("cues") or [])), None)
        if twin is None:
            kept.append(c)
        else:
            gone.append((c, twin))
    keep_ids = {id(k) for k in kept}
    return [c for c in cands if id(c) in keep_ids], gone


# ----------------------------------------------------------------------------- the search

def _title_query(service: str, plan: dict) -> dict:
    if service == "subdl":
        q = {"film_name": plan["title"]}
    else:
        q = {"query": plan["title"]}
    if not plan["movie"]:
        q.update({"season_number": plan["season"] or 1, "episode_number": plan["season_episode"] or plan["episode"]})
    return q


def search_service(service: str, client, log: Log, plan: dict, lang: str) -> dict:
    """One service, the ladder in order, stopping at the first step with a candidate that fits. Never raises."""
    t0 = time.monotonic()
    report = {"service": service, "status": "ok", "steps": [], "kept": [], "dropped": Counter()}
    steps = ladder(plan)
    if plan["tmdb"] is None and plan["imdb"] is None:
        steps = []
    try:
        for name, params in steps + [("title", None)]:
            if name == "title":
                if report["kept"] or not plan["title"]:
                    break
                params = _title_query(service, plan)
            records = client.search(plan, lang, params, name)
            kept, dropped = NORMALIZE[service](records, plan, lang, name, log)
            report["steps"].append({"step": name, "records": len(records), "kept": len(kept)})
            report["dropped"].update(dropped)
            log.info(f"extsubs.{service}", "step", result="PASS" if kept else "EMPTY", step=name,
                     params={k: v for k, v in params.items() if k not in ("film_name", "query")},
                     records=len(records), kept=len(kept), dropped=dict(dropped))
            if kept:
                report["kept"] = kept
                break
    except ServiceSkipped as exc:
        report["status"] = f"skipped: {exc}"
        log.warn(f"extsubs.{service}", "skip", result="FAIL", reason=str(exc),
                 suggest="the other service still answers; this one is tried again next play")
    except StageError as exc:
        report["status"] = f"error: {exc}"
        log.warn(f"extsubs.{service}", "error", result="FAIL", error=str(exc), suggest=exc.suggest)
    except Exception as exc:                            # noqa: BLE001 - the docstring's "never raises"
        report["status"] = f"error: {type(exc).__name__}: {exc}"
        log.warn(f"extsubs.{service}", "error", result="FAIL", error=f"{type(exc).__name__}: {exc}",
                 suggest="an unexpected error in this service's search; the other service still answers")
    report["ms"] = round((time.monotonic() - t0) * 1000)
    return report


def search(http: Http, log: Log, plan: dict, lang: str, *, playing_title: str = "",
           services=SERVICES, tokens: dict | None = None) -> dict:
    """Both services at once; candidates that fit, ranked. Never raises.

    `tokens` overrides the stored keys (tests). A service without a key is reported as such and skipped.
    """
    t0 = time.monotonic()
    if not known_language(lang):
        log.warn("extsubs.search", "language", result="FAIL", lang=lang,
                 suggest=f"no subtitle-database code for {lang}; add it to extsubs.LANG_CODES")
        return {"candidates": [], "services": {}, "lang": lang}
    log.info("extsubs.search", "start", episode=plan["episode"], lang=lang,
             codes={svc: service_codes(lang, svc) for svc in services}, movie=plan["movie"],
             movie_from=plan.get("movie_from"), format_from=plan.get("format_from"),
             tmdb=plan["tmdb"], imdb=plan["imdb"], ids_from=plan["ids_from"], season=plan["season"],
             season_episode=plan["season_episode"], absolute=plan["absolute"], numbering=plan["numbering"],
             years=plan.get("years"), years_from=plan.get("years_from"), sequel=plan.get("sequel"),
             sequel_from=plan.get("sequel_from"),
             movie_pairs=[list(p) for p in sorted(plan["target"]["pairs"])] if plan["movie"] else None,
             ladder=[n for n, _ in ladder(plan)])
    clients, reports = {}, {}
    for svc in services:
        if not service_codes(lang, svc):
            reports[svc] = {"service": svc, "status": "no code", "steps": [], "kept": [], "dropped": Counter()}
            log.info(f"extsubs.{svc}", "language", result="SKIP", lang=lang,
                     reason=f"{svc} has no code for {lang} (extsubs.LANG_CODES)")
            continue
        try:
            token = (tokens or {}).get(svc) or read_token(svc)
        except StageError:
            reports[svc] = {"service": svc, "status": "no key", "steps": [], "kept": [], "dropped": Counter()}
            log.info(f"extsubs.{svc}", "key", result="SKIP", suggest=f"add a {svc} key in Accounts")
            continue
        clients[svc] = CLIENTS[svc](http, log, token)
    with ThreadPoolExecutor(max_workers=max(1, len(clients))) as pool:
        futures = {svc: pool.submit(search_service, svc, c, log, plan, lang) for svc, c in clients.items()}
        for svc, fut in futures.items():
            reports[svc] = fut.result()
    ranked, copies = dedupe_exact(rank([c for r in reports.values() for c in r["kept"]], playing_title))
    log.info("extsubs.search", "done", result="PASS" if ranked else "EMPTY",
             ms=(time.monotonic() - t0) * 1000, candidates=len(ranked), same_md5_dropped=copies,
             per_service={s: {"status": r["status"], "kept": len(r["kept"]), "dropped": dict(r["dropped"])}
                          for s, r in reports.items()},
             top=[{"service": c["service"], "name": c["name"], "affinity": c["affinity"], "code": c.get("code"),
                   "region_rank": c.get("region_rank"),
                   "downloads": c["downloads"], "check": c.get("check"), "show_tie": c.get("show_tie")}
                  for c in ranked[:TOP_N]])
    return {"candidates": ranked, "services": reports, "lang": lang}



# ----------------------------------------------------------------------------- downloads, caches, quota
#
# A file is cached forever by its database id (a rewatch costs nothing); a search for 24 h; a
# service out of quota is skipped until its reset and Accounts says so; a 429 skips, never loops.

SEARCH_TTL_S = 86400
DOWNLOAD_CAP = 5                  # per episode: the top 3, plus two to replace a duplicate or a failure
MAX_FILE_BYTES = 2 * 2**20        # a subtitle file or a zip of one episode's files; measured: 21-40 KB
MAX_UNZIPPED_BYTES = 12 * 2**20
ZIP_READ_BYTES = 64 * 2**10       # one inflate call's output, at most
ZIP_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)      # the recorded Subdl zip is deflated (1 of 1)
FETCH_TIMEOUT_S = 10
SUB_EXTS = (".srt", ".ass", ".ssa")


def ext_dir() -> Path:
    return core.CACHE_DIR / "extsubs"


def file_path(service: str, file_id: str, fmt: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", str(file_id))[:120]
    return ext_dir() / service / f"{safe}.{'ass' if fmt in ('ass', 'ssa') else 'srt'}"


def search_cache_path(anilist_id: int, episode: int, lang: str) -> Path:
    return ext_dir() / "search" / f"{int(anilist_id)}-{int(episode)}-{lang}.json"


# -- quota

def _quota_path() -> Path:
    return ext_dir() / "quota.json"


def quota() -> dict:
    """`{service: {remaining, limit, reset_at, exhausted_until, updated}}`, as last seen. Never raises."""
    got = cached_json(_quota_path(), None)
    return got if isinstance(got, dict) else {}         # a file holding a list is no quota


def _note_quota(log: Log, service: str, **fields) -> None:
    state = quota()
    entry = {**state.get(service, {}), **{k: v for k, v in fields.items() if v is not None}, "updated": time.time()}
    state[service] = entry
    try:
        write_cache(_quota_path(), state)
    except OSError as exc:
        log.warn("extsubs.quota", "write", result="FAIL", error=str(exc))
    log.info("extsubs.quota", "note", service=service, **{k: v for k, v in entry.items() if k != "updated"})


def _parse_utc(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def exhausted(service: str, now: float | None = None) -> bool:
    until = (quota().get(service) or {}).get("exhausted_until")
    return bool(until) and (now or time.time()) < float(until)


# -- the file itself

def _pick_from_zip(log: Log, blob: bytes, cand: dict, plan: dict) -> tuple[str, bytes] | None:
    """A Subdl record link is a zip. Take the one subtitle file in it, or the one whose name fits the
    episode. The declared size is checked first, and the member is then read in 64 KiB pieces that stop past
    MAX_UNZIPPED_BYTES: `zf.read` inflates in one call of up to 2 GiB before cutting to the declared size, which
    a lying header made 3.8 GB out of 1.5 KB.

    A zip is third-party bytes, and reading one raises many kinds (measured: BadZipFile for a bad CRC,
    RuntimeError for an encrypted member, NotImplementedError for an unknown method, zlib.error, LZMAError, OSError
    for bzip2, ValueError for a cut archive). Any of them makes this file unusable: one FAIL line, and None."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except Exception as exc:                            # noqa: BLE001 - see above
        log.warn("extsubs.download", "zip", result="FAIL", file_id=cand["file_id"],
                 error=f"{type(exc).__name__}: {exc}", suggest="the next candidate is tried instead")
        return None
    subs = [i for i in zf.infolist() if i.filename.lower().endswith(SUB_EXTS) and not i.is_dir()]
    if len(subs) > 1:
        checked = [(i, name_check(Path(i.filename).name, plan["variants"], plan["target"], True)) for i in subs]
        subs = [i for i, (ok, _) in checked if ok
                and (plan["target"].get("movie") or named_episodes(Path(i.filename).name)[0] & plan["target"]["numbers"])]
        whole = [i for i, (ok, why) in checked if ok and not why.startswith("ok (movie: set aside")]
        if plan["target"].get("movie") and len(subs) > 1 and len(whole) == 1:
            # A movie's zip holding its file under two names. The file whose whole name fits is the one
            # the check took before the movie reading (the title part) let the other in.
            log.info("extsubs.download", "zip", result="PASS", file_id=cand["file_id"], fitting=len(subs),
                     picked=whole[0].filename, reason="the one name that fits whole; the others fit by the movie reading")
            subs = whole
    if len(subs) != 1:
        log.warn("extsubs.download", "zip", result="FAIL", file_id=cand["file_id"], fitting=len(subs),
                 names=[i.filename for i in zf.infolist()][:8],
                 suggest="a zip must hold exactly one subtitle file that fits this episode")
        return None
    info = subs[0]
    if info.file_size > MAX_UNZIPPED_BYTES:
        log.warn("extsubs.download", "zip", result="FAIL", file_id=cand["file_id"], size=info.file_size)
        return None
    if info.compress_type not in ZIP_METHODS:
        # Python 3.14.7's zipfile inflates a bzip2/lzma/zstd member's whole input in one call however small the
        # read (measured: 318 B -> 400 MB); 3.14.8 and 3.15.1 measured bounded. A deflated member stayed bounded on
        # 3.14.7 and 3.14.8 (measured), and a stored one is copied as it is read.
        log.warn("extsubs.download", "zip", result="FAIL", file_id=cand["file_id"], name=info.filename,
                 method=info.compress_type, allowed=list(ZIP_METHODS),
                 suggest="only stored or deflated members are read; the next candidate is tried")
        return None
    try:
        data = bytearray()
        with zf.open(info) as fh:
            while len(data) <= MAX_UNZIPPED_BYTES:
                piece = fh.read(min(ZIP_READ_BYTES, MAX_UNZIPPED_BYTES + 1 - len(data)))
                if not piece:
                    break
                data += piece
        if len(data) > MAX_UNZIPPED_BYTES:
            log.warn("extsubs.download", "zip", result="FAIL", file_id=cand["file_id"], name=info.filename,
                     declared=info.file_size, read=len(data), limit=MAX_UNZIPPED_BYTES,
                     suggest="the member inflates past its declared size and the limit; the next candidate is tried")
            return None
        return info.filename, bytes(data)
    except Exception as exc:                            # noqa: BLE001 - see the docstring
        log.warn("extsubs.download", "zip", result="FAIL", file_id=cand["file_id"], name=info.filename,
                 error=f"{type(exc).__name__}: {exc}", suggest="the next candidate is tried instead")
        return None


def _fetch(url: str) -> bytes:
    status, blob, _ = get_capped(url, MAX_FILE_BYTES, timeout=FETCH_TIMEOUT_S)
    if status != 200 or not blob:
        raise StageError("extsubs.fetch", f"status {status}, {len(blob)} bytes")
    return blob


def _os_link(http: Http, log: Log, token: str, cand: dict) -> str:
    """OpenSubtitles' two steps: POST /download for a one-off link (this is what counts against the
    100/day), then a plain GET. `remaining` and `reset_time_utc` in every answer keep the quota current."""
    if exhausted("opensubtitles"):
        raise ServiceSkipped("the daily download quota is used up")
    _pace("opensubtitles")
    status, body, hdrs = http.request("extsubs.opensubtitles.download", "POST", f"{OS_API}/download",
                                      headers={"Api-Key": token, "Content-Type": "application/json"},
                                      data=json.dumps({"file_id": int(cand["file_id"])}).encode(),
                                      retries=1, max_retry_after=0)
    body = body if isinstance(body, dict) else {}
    reset = _parse_utc(body.get("reset_time_utc"))
    message = str(body.get("message") or "")
    remaining = body.get("remaining")
    # 406 is also "Invalid file_id". Only a message about the allowance, or `remaining` at 0,
    # is the quota; anything else is this one file.
    if status == 406 and (re.search(r"allowed|quota|download count|limit", message, re.I)
                          or (isinstance(remaining, int) and remaining <= 0)):
        _note_quota(log, "opensubtitles", remaining=0, reset_at=reset, exhausted_until=reset or time.time() + 86400)
        raise ServiceSkipped(f"the daily download quota is used up ({message or 406})")
    if status == 429:
        raise ServiceSkipped("429 on download")
    if status == 503:
        # Measured: an unknown key answers 503 "Service unavailable"; so does a real outage.
        raise ServiceSkipped(f"503 on download ({message or 'service unavailable'}): a wrong key or an outage")
    if status in (401, 403):
        raise ServiceSkipped(f"the key was refused ({status})")
    if status != 200 or not body.get("link"):
        raise StageError("extsubs.opensubtitles.download", f"status {status}: {str(body)[:200]}")
    _note_quota(log, "opensubtitles", remaining=remaining, limit=(remaining or 0) + (body.get("requests") or 0),
                reset_at=reset, exhausted_until=reset if isinstance(remaining, int) and remaining <= 0 else 0)
    return body["link"]


def download(http: Http, log: Log, cand: dict, plan: dict, tokens: dict, lang: str | None = None) -> dict | None:
    """The file for one candidate, from the permanent cache or the service. Never raises except
    ServiceSkipped (the caller stops asking that service). Returns `{path, text, format, cues, cached}`.

    `lang` is the language the file was searched in: a file that is not UTF-8 is read in its codepages,
    and the cached copy is written as UTF-8 (`subs.WRITTEN_ENCODING`).

    Whatever else this candidate raises (a cached copy that cannot be read, a full disk on the write) is
    one FAIL line and None, so `prepare` tries the next candidate; it used to end the external-subtitle worker."""
    try:
        return _download(http, log, cand, plan, tokens, lang)
    except ServiceSkipped:
        raise
    except Exception as exc:                            # noqa: BLE001 - the docstring's contract
        log.warn("extsubs.download", "error", result="FAIL", service=cand.get("service"),
                 file_id=cand.get("file_id"), error=f"{type(exc).__name__}: {exc}",
                 suggest="the next candidate is tried instead")
        return None


def _download(http: Http, log: Log, cand: dict, plan: dict, tokens: dict, lang: str | None) -> dict | None:
    """`download`'s body, which may raise; `download` turns anything but ServiceSkipped into None."""
    t0 = time.monotonic()
    fmt = cand.get("format") or "srt"
    for guess in (fmt, "ass" if fmt == "srt" else "srt"):
        path = file_path(cand["service"], cand["file_id"], guess)
        if path.exists() and path.stat().st_size > 0:
            text, _ = decode_text(path.read_bytes(), lang, log, service=cand["service"], file_id=cand["file_id"],
                                  path=str(path))
            cues = parse_cues(text, guess.upper(), log, str(path))
            log.debug("extsubs.download", "cache", result="HIT", service=cand["service"], file_id=cand["file_id"])
            return {"path": str(path), "text": text, "format": guess, "cues": cues, "cached": True}
    try:
        if cand["service"] == "subdl":
            _pace("subdl")
            blob = _fetch(cand["url"])
        else:
            blob = _fetch(_os_link(http, log, tokens["opensubtitles"], cand))
        name = cand["name"]
        if blob[:2] == b"PK":
            picked = _pick_from_zip(log, blob, cand, plan)
            if not picked:
                return None
            name, blob = picked
    except ServiceSkipped:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise ServiceSkipped(f"429 on download (retry-after {exc.headers.get('Retry-After') or '?'})")
        log.warn("extsubs.download", "fetch", result="FAIL", service=cand["service"], file_id=cand["file_id"],
                 status=exc.code, ms=(time.monotonic() - t0) * 1000, suggest="the next candidate is tried instead")
        return None
    except (StageError, urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warn("extsubs.download", "fetch", result="FAIL", service=cand["service"], file_id=cand["file_id"],
                 error=str(exc), reason=exc.data.get("reason") if isinstance(exc, StageError) else None,
                 ms=(time.monotonic() - t0) * 1000,
                 suggest="the next candidate is tried instead")
        return None
    text, encoding = decode_text(blob, lang, log, service=cand["service"], file_id=cand["file_id"], name=name)
    text, cut, removed = strip_ass_attachments(text)
    if cut:
        log.warn("extsubs.download", "strip", result="WARN", service=cand["service"], file_id=cand["file_id"],
                 sections=cut, chars_removed=removed,
                 suggest="the file's embedded fonts/pictures were left out; its styles name system fonts")
    fmt = "ass" if "[Events]" in text or "Dialogue:" in text else "srt"
    cues = parse_cues(text, fmt.upper(), log, f"{cand['service']}:{cand['file_id']}")
    if len(cues) < 20:
        log.warn("extsubs.download", "parse", result="FAIL", service=cand["service"], file_id=cand["file_id"],
                 cues=len(cues), bytes=len(blob), suggest="not a subtitle file for a whole episode; skipped")
        return None
    path = write_bytes_atomic(file_path(cand["service"], cand["file_id"], fmt), text.encode(WRITTEN_ENCODING))
    log.info("extsubs.download", "done", result="PASS", service=cand["service"], file_id=cand["file_id"],
             name=name, bytes=len(blob), encoding=encoding, written=WRITTEN_ENCODING, format=fmt, cues=len(cues),
             ms=(time.monotonic() - t0) * 1000)
    return {"path": str(path), "text": text, "format": fmt, "cues": cues, "cached": False}


# -- timing

def reference_from_file(path: str, log: Log | None = None) -> list[float]:
    """Cue starts of a subtitle file timed to the playing release (the `--reference` flag). Its language is
    not known, so a file that is not UTF-8 is read as Western; only its cue times are used."""
    text, _ = decode_text(Path(path).read_bytes(), None, log, path=str(path))
    fmt = "ASS" if "Dialogue:" in text else "SRT"
    return [a for a, _ in parse_cues(text, fmt, log, str(path))]


def time_against(log: Log, cand: dict, cues: list[tuple[float, float]], reference: list[float] | None) -> dict:
    """`subs.offset_decision`'s judgement with the external window (180 s). No reference: unverified, loaded
    unshifted."""
    if not reference:
        return {"offset": 0.0, "timing_verified": None, "match_ratio": None,
                "reason": "no timing reference for the playing release"}
    got = offset_decision([a for a, _ in cues], reference, EXTERNAL_MAX_OFFSET_S, log)
    log.info("extsubs.timing", "decide", result="PASS" if got["timing_verified"] else "FAIL",
             service=cand["service"], file_id=cand["file_id"], name=cand["name"], offset=got["offset"],
             match_ratio=got["match_ratio"], bucket_span=got.get("bucket_span"),
             local_offsets=got.get("local_offsets"), reason=got["reason"])
    return {k: got.get(k) for k in ("offset", "timing_verified", "match_ratio", "bucket_span", "reason")}


REFERENCE_MIN_SHARE = 0.5


def usable_reference(log: Log, reference: list[float] | None, file_cues: list[int]) -> list[float] | None:
    """A reference far shorter than the files is the wrong track (the first live run used a 77-cue signs
    track against 184-302-cue files and read every good file as "a different episode"). Then there is
    no reference: the files load unverified instead of rejected for the wrong reason."""
    if not reference or not file_cues:
        return reference or None
    median = sorted(file_cues)[len(file_cues) // 2]
    if len(reference) < REFERENCE_MIN_SHARE * median:
        log.warn("extsubs.timing", "reference", result="FAIL", reference_cues=len(reference), file_cues=file_cues,
                 suggest="the timing reference is too short to be the dialogue track; files load unverified")
        return None
    return reference


def _title_step_ties(cands) -> list[dict]:
    """Each title-step candidate's tie as the cache keeps it (name, tie, reason, the record's ids), for a log line."""
    return [{"name": _cut(str(c.get("name") or "")), "tie": c.get("show_tie"), "why": c.get("show_tie_why"),
             "ids": c.get("show_ids")} for c in cands if isinstance(c, dict) and c.get("step") == "title"][:TOP_N]


def _search_cached(http: Http, log: Log, plan: dict, lang: str, anilist_id: int, playing_title: str,
                   tokens: dict | None) -> dict:
    """`search`, cached 24 h per (show, episode, language). Ranking is recomputed from the cached
    candidates, because it depends on the playing release."""
    path = search_cache_path(anilist_id, plan["episode"], lang)
    hit = cached_json(path, SEARCH_TTL_S)
    stale = [c.get("name") for c in (hit or {}).get("candidates") or [] if isinstance(c, dict)
             and c.get("step") == "title" and "show_tie" not in c] if isinstance(hit, dict) else []
    if stale:
        # Written before title-step files were checked against the entry's ids: those files never were.
        log.info("extsubs.search", "cache", result="STALE", path=str(path), title_step=len(stale),
                 names=[_cut(str(n)) for n in stale[:TOP_N]],
                 reason="title-step files cached before their show ids were checked: searched again")
        hit = None
    if hit and isinstance(hit.get("candidates"), list):
        log.info("extsubs.search", "cache", result="HIT", path=str(path), candidates=len(hit["candidates"]),
                 years=plan.get("years"), movie=plan.get("movie"), movie_from=plan.get("movie_from"),
                 format_from=plan.get("format_from"), title_step=_title_step_ties(hit["candidates"]) or None)
        ranked, _ = dedupe_exact(rank(hit["candidates"], playing_title))
        return {"candidates": ranked, "services": hit.get("services") or {}, "lang": lang, "cached": True}
    found = search(http, log, plan, lang, playing_title=playing_title, tokens=tokens)
    services = {s: {"status": r["status"], "steps": r["steps"], "kept": len(r["kept"]),
                    "dropped": dict(r["dropped"])} for s, r in found["services"].items()}
    if all(r["status"] in ("ok", "no key") for r in found["services"].values()):
        try:
            write_cache(path, {"candidates": [{k: v for k, v in c.items() if k not in ("affinity", "affinity_why")}
                                              for c in found["candidates"]], "services": services})
        except OSError as exc:                          # a full disk costs the cache, not the search
            log.warn("extsubs.search", "cache_write", result="FAIL", path=str(path), error=str(exc),
                     suggest="the search is used this play and asked again next play")
    return {"candidates": found["candidates"], "services": services, "lang": lang, "cached": False}


def prepare(http: Http, log: Log, plan: dict, lang: str, anilist_id: int, *, playing_title: str = "",
            reference: list[float] | None = None, tokens: dict | None = None) -> dict:
    """Search (cached), download the best files until three distinct ones are in hand, time each against
    the reference, rank again with timing and write the shifted copies. Never raises.

    Returns `{tracks: [...up to 3], candidates, services, downloads, skipped}`; each track carries
    `path` (the file to load: shifted when verified), `offset`, `timing_verified`, `service`, `name`.
    """
    t0 = time.monotonic()
    tokens = dict(tokens or {})
    for svc in SERVICES:
        if svc not in tokens:
            try:
                tokens[svc] = read_token(svc)
            except StageError:
                pass
    try:
        found = _search_cached(http, log, plan, lang, anilist_id, playing_title, tokens)
    except Exception as exc:                            # noqa: BLE001 - the docstring's "never raises"
        log.warn("extsubs.search", "error", result="FAIL", error=f"{type(exc).__name__}: {exc}",
                 suggest="no candidates this play (writing the search cache can fail on a full disk)")
        found = {"candidates": [], "services": {}, "lang": lang, "cached": False}
    have: list[dict] = []
    skipped: dict[str, str] = {}
    downloads = 0
    for cand in found["candidates"]:
        if len(have) >= TOP_N or downloads >= DOWNLOAD_CAP:
            break
        if cand["service"] in skipped:
            continue
        if cand["service"] == "opensubtitles" and "opensubtitles" not in tokens:
            skipped["opensubtitles"] = "no key"
            continue
        try:
            got = download(http, log, cand, plan, tokens, lang=lang)
        except ServiceSkipped as exc:
            skipped[cand["service"]] = str(exc)
            log.warn(f"extsubs.{cand['service']}", "skip", result="FAIL", reason=str(exc), during="download",
                     suggest="the other service's files are used; this one is tried again next play")
            continue
        if not got:
            continue
        downloads += 0 if got["cached"] else 1
        cues = [a for a, _ in got["cues"]]
        twin = next((h for h in have if same_file(cues, h["cue_starts"], log)), None)
        if twin:
            # Files are tried in rank order, so the copy already kept is the better-ranked one.
            log.info("extsubs.dedupe", "same", result="PASS", dropped=cand["name"], kept=twin["name"],
                     dropped_service=cand["service"], kept_service=twin["service"])
            continue
        have.append({**cand, "raw_path": got["path"], "text": got["text"], "file_format": got["format"],
                     "cue_starts": cues, "cues": len(cues), "cached": got["cached"], "_cues": got["cues"]})
    reference = usable_reference(log, reference, [h["cues"] for h in have])
    for h in have:
        h.update(time_against(log, h, h.pop("_cues"), reference))
    tracks = sorted(have, key=rank_key, reverse=True)
    for t in tracks:
        t["path"] = _placed(log, anilist_id, plan["episode"], lang, t)
    log.info("extsubs.prepare", "done", result="PASS" if tracks else "EMPTY", ms=(time.monotonic() - t0) * 1000,
             tracks=[{"service": t["service"], "name": t["name"], "offset": t["offset"],
                      "verified": t["timing_verified"]} for t in tracks],
             downloads=downloads, skipped=skipped, search_cached=found.get("cached"))
    return {"tracks": [{k: v for k, v in t.items() if k not in ("text", "cue_starts")} for t in tracks],
            "candidates": found["candidates"], "services": found["services"], "downloads": downloads,
            "skipped": skipped}


def _placed(log: Log, anilist_id: int, episode: int, lang: str, t: dict) -> str:
    """The file mpv gets: the cached original when unshifted, else a shifted copy beside it."""
    if not t.get("timing_verified") or not t.get("offset"):
        return t["raw_path"]
    out = ext_dir() / "tracks" / f"{int(anilist_id)}-{int(episode)}-{lang}-{t['service']}-{Path(t['raw_path']).name}"
    try:
        write_bytes_atomic(out, shift_text(t["text"], t["file_format"].upper(), t["offset"]).encode(WRITTEN_ENCODING))
    except OSError as exc:
        log.warn("extsubs.place", "write", result="FAIL", error=str(exc), path=str(out),
                 suggest="the unshifted file loads instead, flagged")
        t["timing_verified"] = None
        return t["raw_path"]
    return str(out)


# Housekeeping. A downloaded file is kept forever (a rewatch costs no quota), so `subdl/`,
# `opensubtitles/` and `quota.json` are never touched here. What does go: a shifted copy in `tracks/`
# (re-derived from the original with no network call, as `subs.prune` treats its retimed files) once
# untouched for TRACKS_MAX_AGE_DAYS, and a search answer past SEARCH_TTL_S (it is refetched anyway).
TRACKS_MAX_AGE_DAYS = 30.0
PRUNE_EVERY_S = 86400
PRUNE_STAMP = ".pruned"


def prune(tracks_max_age_days: float = TRACKS_MAX_AGE_DAYS, search_max_age_s: float = SEARCH_TTL_S) -> dict:
    t0 = time.monotonic()
    now = time.time()
    removed, freed = 0, 0
    for sub, cutoff in (("tracks", now - tracks_max_age_days * 86400), ("search", now - search_max_age_s)):
        try:
            files = [p for p in (ext_dir() / sub).iterdir() if p.is_file()]
        except OSError:
            continue
        for p in files:
            try:
                st = p.stat()
                if st.st_mtime < cutoff and st.st_atime < cutoff:
                    p.unlink()
                    removed += 1
                    freed += st.st_size
            except OSError:
                continue
    return {"removed": removed, "freed": freed, "ms": round((time.monotonic() - t0) * 1000, 1)}


def maybe_prune(log: Log | None = None) -> dict | None:
    """At most once a day, behind a stamp file, like `subs.maybe_prune`."""
    stamp = ext_dir() / PRUNE_STAMP
    try:
        if time.time() - stamp.stat().st_mtime < PRUNE_EVERY_S:
            return None
    except OSError:
        pass
    core.touch_private(stamp)
    out = prune()
    if log is not None:
        log.info("extsubs.prune", "daily", result="PASS", **out)
    return out



# ----------------------------------------------------------------------------- the reference and the player

REFERENCE_LANG_ORDER = ("eng", "spa", "por", "fre", "ger", "ita")
_SIGNS = re.compile(r"sign|song|karaoke|forced|lyrics", re.I)


def reference_from_releases(log: Log, track_source, releases: list[dict]) -> tuple[list[float] | None, dict]:
    """The playing release's own text track, else one of a same-source release of the same episode,
    in the order given. `releases` are `{indexer, release_id}`; their tracks and text come through the
    subtitle-track capability (`track_source`, an `indexers.SubtitleTracks`), and a release whose
    indexer does not offer it is skipped. Never a forced or signs track; of a release's usable tracks the
    one with the most cues wins. Returns `(cue starts or None, what was used)`. Never raises."""
    from .subs import fetch_track, normalize_lang
    for ref in releases:
        who = release_key(ref).as_dict()
        rec = track_source.read(who) if track_source is not None else None
        if rec is None:
            log.warn("extsubs.reference", "tracks", result="SKIP", **who,
                     suggest="no loaded indexer add-on offers subtitle tracks for this release")
            continue
        usable = [t for t in rec["tracks"] if t.get("url") and not t.get("forced")
                  and (t.get("format") or "").upper() in SUBTITLE_FORMATS and not _SIGNS.search(t.get("name") or "")]
        usable.sort(key=lambda t: (REFERENCE_LANG_ORDER.index(normalize_lang(t.get("lang")))
                                   if normalize_lang(t.get("lang")) in REFERENCE_LANG_ORDER else 99))
        best, best_cues = None, []
        for t in usable[:2]:
            got = fetch_track(log, track_source, {**who, "track_id": t["id"], "format": t["format"], "url": t["url"],
                                            "lang": t.get("lang"), "name": t.get("name")})
            if not got:
                continue
            try:
                cues = [a for a, _ in parse_cues(got["text"], got["format"], log, got["path"])]
            except Exception as exc:                # noqa: BLE001 - the docstring's "never raises"
                log.warn("extsubs.reference", "parse", result="FAIL", **who, track_id=t["id"],
                         error=f"{type(exc).__name__}: {exc}", suggest="this track is skipped as a timing reference")
                continue
            if len(cues) > len(best_cues):
                best, best_cues = t, cues
        if best:
            log.info("extsubs.reference", "pick", result="PASS", **who, lang=best.get("lang"),
                     name=best.get("name"), format=best.get("format"), cues=len(best_cues))
            return best_cues, {**who, "lang": best.get("lang"), "cues": len(best_cues)}
        log.info("extsubs.reference", "none", result="EMPTY", **who, tracks=len(rec["tracks"]), status=rec["status"])
    return None, {}


SERVICE_NAMES = {"subdl": "Subdl", "opensubtitles": "OpenSubtitles"}


def track_title(lang: str, t: dict) -> str:
    """mpv's track title names the language, the source and the timing state."""
    from .subs import language_name
    parts = [language_name(file_language(lang, t)), SERVICE_NAMES.get(t["service"], t["service"])]
    # Live 2026-09-26: two tracks both read "Arabic · Subdl · unverified", so `j` could not tell them
    # apart. The release group in the file's name (else the uploader) does.
    who = parse_title(t.get("name") or "").get("group") or t.get("uploader") or ""
    if who:
        parts.append(str(who)[:24])
    if t.get("machine"):
        parts.append("machine-translated")
    if t.get("timing_verified") and t.get("offset"):
        parts.append(f"shifted {t['offset']:+.1f} s")
    elif not t.get("timing_verified"):
        parts.append("unverified")
    if untied(t):
        parts.append("may be another show")
    return " · ".join(parts)


def external_badge(lang: str, t: dict, added: int) -> str:
    from .subs import language_name
    who = SERVICE_NAMES.get(t["service"], t["service"])
    line = f"{language_name(file_language(lang, t))} from {who}"
    if t.get("machine"):
        line += " · machine-translated"
    if t.get("timing_verified") and t.get("offset"):
        line += f" · shifted {t['offset']:+.1f} s"
    elif not t.get("timing_verified"):
        line += " · timing unverified — nudge it below"
    if added > 1:
        line += f" · {added - 1} more with j"
    return line


def still_playing(expect: dict, mpv=None) -> dict:
    """The worker outlives the play command; a new play for another episode must not get these tracks.

    now-playing.json is rewritten seconds after the next play's `loadfile`, so with `mpv` the
    player stamp is checked too; a player that has no stamp is judged by now-playing alone. Returns the
    decision, `{"playing", "by": "now-playing"|"player stamp", "reason", "stamp"}`, for the caller's log line."""
    from .player import read_now_playing, read_stamp, stamp_matches
    np = read_now_playing() or {}
    if not (np.get("mediaId") == expect.get("mediaId") and np.get("episode") == expect.get("episode")
            and str(np.get("infohash") or "").lower() == str(expect.get("infohash") or "").lower()):
        return {"playing": False, "by": "now-playing", "stamp": None,
                "reason": f"now-playing names {np.get('mediaId')}:{np.get('episode')}"}
    if mpv is None:
        return {"playing": True, "by": "now-playing", "stamp": None, "reason": "now-playing names this episode"}
    read = read_stamp(mpv)
    same = stamp_matches(read, expect.get("mediaId") or 0, expect.get("episode") or 0, info_hash=expect.get("infohash"))
    return {"playing": same["match"] is not False, "by": "player stamp", "stamp": read.get("stamp"),
            "reason": same["reason"]}


def load_into_player(log: Log, mpv, tracks: list[dict], lang: str, expect: dict, plugin_sid,
                     may_replace: bool) -> dict:
    """Add the tracks to the running player, best first. The best is selected only when the
    viewer has not picked a track since the plugin set one (`plugin_sid`) and either nothing in the
    language is on or `may_replace` (what is on is an unverified donor and the best file is verified), and never
    when nothing ties it to the entry's show (`untied`: a title search also finds other shows): such a file is
    added for the viewer to choose (`offered`). Checks the episode before every `sub-add`. Never raises; returns what
    happened."""
    out = {"added": [], "selected": None, "stopped": None, "offered": None}
    try:
        now_sid = mpv.command("get_property", "sid")
    except (StageError, OSError) as exc:                # a player that quit resets the socket (OSError)
        out["stopped"] = f"mpv did not answer: {exc}"
        log.warn("extsubs.load", "sid", result="FAIL", error=f"{type(exc).__name__}: {exc}", tracks=len(tracks),
                 suggest="the player is gone or not answering; nothing was loaded")
        return out
    viewer_chose = str(now_sid) != str(plugin_sid)
    for i, t in enumerate(tracks):
        check = still_playing(expect, mpv)
        if not check["playing"]:
            out["stopped"] = "another episode is playing"
            log.info("extsubs.load", "stop", result="SKIP", reason=out["stopped"], added=len(out["added"]),
                     by=check["by"], why=check["reason"], stamp=check["stamp"])
            break
        select = i == 0 and not viewer_chose and may_replace
        if select and untied(t):
            select = False
            out["offered"] = f"found by title only: {t.get('show_tie_why') or 'nothing ties it to the show'}"
        title = track_title(lang, t)
        try:
            mpv.command("sub-add", t["path"], "select" if select else "auto", title, lang)
        except (StageError, OSError) as exc:
            log.warn("extsubs.load", "sub-add", result="FAIL", path=t["path"], error=str(exc))
            continue
        out["added"].append({"title": title, "path": t["path"], "selected": select})
        if select:
            readback = {}
            for key in ("lang", "external", "title"):
                try:
                    readback[key] = mpv.command("get_property", f"current-tracks/sub/{key}")
                except (StageError, OSError):
                    readback[key] = None
            ok = readback.get("external") is True and readback.get("title") == title
            out["selected"] = title if ok else None
            log.emit("info" if ok else "warn", "extsubs.load", "select", result="PASS" if ok else "FAIL",
                     title=title, readback=readback)
    log.info("extsubs.load", "done", result="PASS" if out["added"] else "EMPTY", added=[a["title"] for a in out["added"]],
             selected=out["selected"], viewer_chose=viewer_chose, plugin_sid=plugin_sid, sid_now=now_sid,
             may_replace=may_replace, stopped=out["stopped"], offered=out["offered"],
             untied=sum(1 for t in tracks if untied(t)), untied_why=_title_step_ties(t for t in tracks if untied(t)))
    return out


def update_now_playing(log: Log, expect: dict, lang: str, tracks: list[dict], loaded: dict, mpv=None) -> bool:
    """The popout's badge: when a track was selected, or when files were added but left off as `untied` (the badge
    alone then says they are there, off); only if the episode is unchanged (with `mpv`, the player stamp too)."""
    from .player import read_now_playing, write_now_playing
    offered = bool(loaded.get("offered")) and not loaded.get("selected") and bool(loaded.get("added"))
    if not loaded.get("selected") and not offered:
        return False
    check = still_playing(expect, mpv)
    if not check["playing"]:
        log.info("nowplaying", "external", result="SKIP", by=check["by"], why=check["reason"], stamp=check["stamp"],
                 suggest="the badge is not written: another file is playing")
        return False
    best = tracks[0]
    np = read_now_playing() or {}
    if offered:
        # Only the line: what is on (nothing, or what the play chose) stays as the play wrote it.
        from .subs import language_name
        np["subBadge"] = (f"{language_name(file_language(lang, best))} from "
                          f"{SERVICE_NAMES.get(best['service'], best['service'])} found by title only, may be "
                          f"another show · not turned on · {len(loaded['added'])} with j")
        write_now_playing(np)
        log.info("nowplaying", "external", result="OFFERED", badge=np["subBadge"], reason=loaded["offered"])
        return True
    np.update({"subSource": "external", "subLang": lang, "subFallback": False,
               "subOffset": round(float(best.get("offset") or 0.0), 3),
               "subTimingVerified": best.get("timing_verified"),
               "subDonor": best["name"], "subDonorSource": SERVICE_NAMES.get(best["service"]),
               "subForced": False, "subBadge": external_badge(lang, best, len(loaded["added"])),
               "subExternal": {"service": best["service"], "uploader": best.get("uploader"),
                               "verified": best.get("timing_verified"), "tracks": len(loaded["added"])}})
    write_now_playing(np)
    log.info("nowplaying", "external", result="PASS", badge=np["subBadge"])
    return True


# ----------------------------------------------------------------------------- `require`

def require_lookup(http: Http, log: Log, mapping: dict, episode: int, variants: list[str], lang: str,
                   anilist_id: int, playing_title: str = "", season_year: int | None = None,
                   anilist_format: str = "", anilist_titles=()) -> dict | None:
    """Under `subtitlePolicy=require` with no donor: does a subtitle database have a file that fits this
    episode? Search only (cached 24 h, shared with the play's worker), nothing downloaded. Never raises.
    `season_year` and `anilist_format` come from the titles cache, as they do for the worker's plan. A file nothing
    ties to the entry's show (`untied`) is not one: the worker would never turn it on."""
    try:
        plan = episode_plan(mapping, episode, variants, season_year=season_year, anilist_format=anilist_format,
                            log=log, anilist_titles=anilist_titles)
        found = _search_cached(http, log, plan, lang, anilist_id, playing_title, None)
    except Exception as exc:                          # noqa: BLE001 - a failed lookup reads as "none found"
        log.warn("extsubs.require", "lookup", result="FAIL", error=f"{type(exc).__name__}: {exc}")
        return None
    cands = found.get("candidates") or []
    best = next((c for c in cands if not untied(c)), None)
    log.info("extsubs.require", "lookup", result="PASS" if best else "EMPTY", movie=plan["movie"],
             movie_from=plan.get("movie_from"), format_from=plan.get("format_from"),
             candidates=len(cands), untied=sum(1 for c in cands if untied(c)), best=best and best["name"],
             services={k: v.get("status") for k, v in (found.get("services") or {}).items()})
    return {k: best.get(k) for k in ("service", "name", "file_id", "downloads")} if best else None


# ----------------------------------------------------------------------------- Accounts

SUBDL_ME = "https://api.subdl.com/api/v2/me"


def verify_key(service: str, token: str, http: Http, log: Log) -> dict:
    """Is this key accepted, and what is left of today's allowance? Raises StageError when refused.

    Subdl: `GET /api/v2/me` with a bearer key (401 for a wrong one) gives the plan and the day's usage.
    Its downloads go keyless, so the number that limits us is the searches (2,000/day free).
    OpenSubtitles: no read endpoint checks the key; `POST /download` for file id 1 answers
    406 "Invalid file_id" to a valid key (nothing is downloaded) and 503 to an unknown one. Its quota
    is the one the last real download reported.
    """
    if service == "subdl":
        status, body, _ = http.request("extsubs.subdl.me", "GET", SUBDL_ME,
                                       headers={"Authorization": f"Bearer {token}"}, retries=1, max_retry_after=0)
        if status in (401, 403):
            raise StageError("auth.verify", "Subdl did not recognise that key",
                             "copy the key from https://subdl.com/panel/api")
        if status != 200 or not isinstance(body, dict):
            raise StageError("auth.verify", f"Subdl answered {status}", "try again in a minute")
        usage = body.get("usage") or {}
        searches, downloads = usage.get("search") or {}, usage.get("downloads") or {}
        _note_quota(log, "subdl", remaining=searches.get("remaining"), limit=searches.get("limit"),
                    reset_at=_parse_utc(searches.get("reset_at")))
        return {"plan": (body.get("plan") or {}).get("name"),
                "quota": {"kind": "searches", "remaining": searches.get("remaining"), "limit": searches.get("limit"),
                          "resetsAt": searches.get("reset_at"),
                          "keyedDownloadsRemaining": downloads.get("remaining")}}
    if service == "opensubtitles":
        status, body, _ = http.request("extsubs.opensubtitles.verify", "POST", f"{OS_API}/download",
                                       headers={"Api-Key": token, "Content-Type": "application/json"},
                                       data=json.dumps({"file_id": 1}).encode(), retries=1, max_retry_after=0)
        message = str((body or {}).get("message") or "") if isinstance(body, dict) else ""
        if status == 406 and "file_id" in message.lower():
            q = quota().get("opensubtitles") or {}
            fresh = q.get("reset_at") and float(q["reset_at"]) > time.time()
            return {"quota": {"kind": "downloads", "remaining": q.get("remaining") if fresh else None,
                              "limit": q.get("limit") or 100,
                              "resetsAt": datetime.fromtimestamp(float(q["reset_at"]), timezone.utc).isoformat()
                              if fresh else None, "exhausted": exhausted("opensubtitles")}}
        if status in (401, 403, 503):
            raise StageError("auth.verify", f"OpenSubtitles did not accept that key ({status} {message})".strip(),
                             "copy the API key of your consumer at https://www.opensubtitles.com/en/consumers")
        raise StageError("auth.verify", f"OpenSubtitles answered {status} {message}".strip(), "try again in a minute")
    raise StageError("auth.verify", f"unknown subtitle service {service}")
