"""Title parsing and the release scoring formula."""
from __future__ import annotations

import difflib
import functools
import json
import math
import os
import random
import re
import socket
import subprocess
import sys
import types
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from typing import TYPE_CHECKING, Iterable, NamedTuple

from .core import (Http, Log, LANGUAGE_TABLE, CACHE_DIR, DEFAULT_SETTINGS, FALLBACK_OFF, GROUP_POINTS_BOUNDS,
                   NUMBER_DIGITS_MAX, as_bool, as_int, as_list, cached_json, fallback_language, file_size,
                   parse_group_entry, same_language, write_cache)
from .anizip import episode_pair

if TYPE_CHECKING:                       # indexers imports this module lazily; the hint needs no import
    from .indexers import SubtitleTracks

# ----------------------------------------------------------------------------- title parsing and scoring

_RES = re.compile(r"(2160p|4k|1080p|720p|480p)", re.I)
_HEVC = re.compile(r"\b(hevc|x265|h\.?265|av1)\b", re.I)
_MULTISUB = re.compile(r"multi[\s._-]?subs?|multiple[\s._-]?sub(?:s|titles?)?\b|\bmulti\b", re.I)
_CR = re.compile(r"\bCR\b|crunchyroll", re.I)
_SOURCE = re.compile(r"\b(CR|Crunchyroll|DSNP|Disney\+?|NF|Netflix|AMZN|Amazon|BILI|Bilibili|IQ|iQIYI|HIDIVE|ADN|B-?Global|WEB|BD|BluRay|DVD)\b", re.I)
_DUB = re.compile(r"english[\s._-]?dub|dub[\s._-]?only|\bdubbed\b", re.I)
_HARDSUB = re.compile(r"hard[\s._-]?subs?|burned", re.I)
# An episode range must stand alone: "001-026", "(01~12)". A codec/resolution token such as "h264-720p" or
# "x265-10bit" has a letter glued to the first number and is not a range (observed 2026-09-15, Lantern x Lantern).
_BATCH = re.compile(r"\bbatch\b|(?<![\w.])\d{1,4}\s*[~-]\s*\d{1,4}(?![\w.]*p\b)", re.I)
# `Part 2 - 10` and `Cour 2 - 01` are a part's statement and then the episode marker, not a range `2 - 10` (which
# read a later part's single as a pack). The statement is set aside before `_BATCH` is asked, so a range after the
# marker (`Part 2 - 01 ~ 13`) still makes a pack and an unspaced `Part 01-06` stays a range of parts. It is set aside
# as a word, so a number before it is not left against the marker (`Season 3 Part 2 - 05` is no `3 - 05`).
# `_MARKED_STATEMENT` below sets aside every singular label's number so, for every reader.
# A label's number is spelled once, `_LABEL_NUMBER`, for the part statement (`_PART_STATED`, `_PACK_PART`'s core) and
# the statement before the marker (`_MARKED_STATEMENT`). The pack identity reading (`_ID_PARTS`, `_ID_JOIN`) keeps its
# own patterns; it agrees with these by measurement.
_LABEL_NUMBER = r"\s*0*\d{1,2}"
_PART_STATED = r"\b(?:part|cour)" + _LABEL_NUMBER
# The two questions about a label's number, side by side. `_RANGE_LABELS` take a range right after them
# (`Part 01-06` is six parts, `Seasons 1-3`, `Vol. 1-4`): `pack_ranges` refuses it as an episode range.
# `_STATED_LABELS`, the singular ones, state one number, so the spaced ` - ` after it is the episode marker
# (`Season 2 - 01`, `Part 2 - 10`, `S2 - 01`): `pack_range_search` sets that number aside. A plural or a volume label
# takes a range (`Seasons 1 - 3`, `Vol. 1 - 3`): it is left, and stays a pack.
_RANGE_LABELS = r"\b(?:part|cour|seasons?|s|vol(?:ume)?\.?|batch)"
_STATED_LABELS = r"\b(?:part|cour|season|s)"
# A statement right before the spaced ` - ` marker is not a range's first number: a label's number (`Season 2 - 01`,
# `Part 2 - 10`, `Cour 2 - 01`). A year (`Show 2011 - 006`) needs no pattern of its own: before a 1-3 digit marker it
# is always the larger number, which the descending rule below refuses.
_MARKED_STATEMENT = re.compile(r"(?P<label>" + _STATED_LABELS + r")" + _LABEL_NUMBER + r"(?=\s+-\s+\d)", re.I)
_SPACED_RANGE = re.compile(r"(\d{1,4})\s+-\s+(\d{1,4})")


def pack_range_search(text: str):
    """The one reading of an episode range in a name, `_BATCH`'s match or None, for `parse_title`, the episode test
    (`_episode_read`) and the franchise part rule. A statement before the spaced ` - ` marker (`_MARKED_STATEMENT`) is
    set aside first, a label's as its word, so `Season 3 Part 2 - 05` is no `3 - 05` and `Part 2 - 01 ~ 13` is still a
    pack. A spaced `<n> - <m>` whose `n` is larger is no range either:
    the number is the title's or a year, and `m` the episode (`Lantern Zoku 2 - 01`, `Heron Psychic 100 - 01`,
    `Show 2011 - 006`); `pack_ranges` refuses it the same way."""
    t = _MARKED_STATEMENT.sub(lambda m: f" {m.group('label')} ", str(text or ""))
    pos = 0
    while (m := _BATCH.search(t, pos)) is not None:
        spaced = _SPACED_RANGE.fullmatch(m.group(0))
        if not spaced or int(spaced.group(1)) <= int(spaced.group(2)):
            return m
        pos = m.start() + len(spaced.group(1))
    return None


_GROUP_LEAD = re.compile(r"^\[([^\]]+)\]")
# Scene-style trailing group: "...H.264-QORVX", optionally followed by "(Multi-Subs)" or an extension.
_GROUP_TRAIL = re.compile(r"-([A-Za-z0-9]{2,12})(?:\.mkv|\.mp4)?(?:\s*\([^)]*\))?\s*$")


_HASH_TAG = re.compile(r"\[[0-9A-Fa-f]{8}\]")
_RES_TOKEN = re.compile(r"\b\d{3,4}p\b|\b4k\b", re.I)
_EP_TOKENS = re.compile(r"(?:(?<=\bS\d)|(?<=\bS\d\d))E(\d{1,4})\b|\bEP?\.?\s?(\d{1,4})\b|(?<![\w.])(\d{1,4})(?:v\d)?(?![\w.])", re.I)


def _episode_text(title: str) -> str:
    """The name the episode readers read: CRC tags, resolutions and `(19xx)`/`(20xx)` years gone, `_` a space."""
    t = _HASH_TAG.sub(" ", str(title or "").replace("_", " "))
    t = _RES_TOKEN.sub(" ", t)
    return re.sub(r"\((19|20)\d\d\)", " ", t)


def _episode_tokens(text: str) -> list[tuple[int, str]]:
    """Every number `_EP_TOKENS` reads in an already cleaned name, with the text it was read from."""
    return [(int(next(g for g in m.groups() if g)), m.group(0).strip()) for m in _EP_TOKENS.finditer(text)]


# A range written in words is a pack's too (`01 a 12`, `01 to 12`, `01 al 12`, `1 bis 12`,
# `01 até 12`, `01 ao 12`). Not in `pack_range_search`, which `parse_title` reads: only the episode test reads it as a
# pack.
_WORDED_RANGE = re.compile(r"(?<![\w.])\d{1,4}\s+(?:a|to|al|bis|até|ao)\s+\d{1,4}(?![\w.])", re.I)
LOG_ANY, LOG_PACK = "any", "pack"       # `_episode_read`'s log when every number decided


def _episode_read(title: str, any_number: bool = False) -> tuple[frozenset, dict]:
    """The numbers `title_matches_episode` takes for a name, and the log of that reading (`title_episode_log`),
    from one decision. Every number of the name (`by` "any" for `any_number`, "pack" for a pack name, "bare" for a name
    with no episode marker), else the marker's numbers (`episode_reading` without titles, `EpisodeReading.as_log`)."""
    text = str(title or "")
    pack = bool(pack_range_search(text) or _WORDED_RANGE.search(text))
    if not any_number and not pack:
        reading = episode_reading(text)
        if reading.by != READ_BARE:
            return reading.numbers, reading.as_log()
    numbers = frozenset(n for n, _ in _episode_tokens(_episode_text(text)))
    by = LOG_ANY if any_number else LOG_PACK if pack else READ_BARE
    return numbers, {"by": by, "numbers": sorted(numbers)}


def title_matches_episode(title: str, episode: int, absolute: int | Iterable[int] | None = None,
                          any_number: bool = False) -> bool:
    """True when the release title names this episode (or its absolute number when given). Resolution tokens (1080p),
    CRC hashes ([00000100]) and years inside parentheses are ignored; `_` reads as a space
    (`Hakuu_Kyoukai_06_`).

    A single is read by its episode marker (`episode_reading`, without titles): any number of the name
    used to count, so a codec or bit depth passed another episode (`S04E23 … HEVC - 10` for episode 10, `[HEVC 10
    bits]`). Three readings keep any number, as before (`_episode_read`): a pack name (`pack_range_search`,
    `parse_title`'s `batch` without a season, or a range in words, `_WORDED_RANGE`), whose range the marker reading
    takes one end of (`01 - 25`, `#01 - 04`, `Parts 1 - 4`, `01 a 12`); a name with no episode marker
    (`episode_reading`'s bare tier: `Show Season 1 [BD]`, `Show - Vol. 1`, mostly a pack that holds the episode its
    number names); and `any_number`, for a caller whose names need not state the episode (a movie's or a one-episode
    entry's) or that asks whether a stray number was it (`pick_episode_file`'s `number_not_episode`)."""
    wanted = {int(episode)}
    if absolute:
        wanted.update(int(a) for a in (absolute if isinstance(absolute, (list, tuple, set)) else [absolute]))
    return bool(_episode_read(title, any_number)[0] & wanted)


def title_episode_log(title: str, any_number: bool = False) -> dict:
    """How `title_matches_episode` read a name, for the line that drops or labels it on the episode;
    the caller passes the `any_number` it decided with (`_episode_read`)."""
    return _episode_read(title, any_number)[1]


# The episode marker of a pack's file name. A code (`S02E05`, `E05`, `EP 05`, `Episode 05`) first, else
# the first dash marker that is not a year (` - 05`, ` - 03~04`; a range only through `~` or an unspaced `-`, so
# `- 04 - 5 Rivers` is 4). A marker keeps the numbers written right after it: a range's end (`S04E29-35`, `Ep 01-13`),
# a bracketed absolute number (`S04E10 (69)`, `S04E17[76]`, `- 10 (69)`), a joined episode (`- 05 & 06`, `- 01 e 02`),
# and after a season code Sonarr's ` - 067 - ` absolute number.
# The release filter (`title_matches_episode`) and the franchise rule's bare fallback (`franchise_words`) read it too.
# The other readers keep their own patterns, each measured against this one over the 4,604 corpus names (2026-10-03):
# `_MARK_*` (the movie's markers, and the related-show rule's `_MARK_NUMBER` over its strings) asks whether a name
# states any marker, a range (`01-24`), an `NxNN` pair and the movie's own pair included: 250 names answer otherwise.
# `_PACK_EPISODE` (the season pack reading) finds a marker this reading does not (`28.5`, `第41话`, `S03E10v2`): 14 names,
# all singles a season pack test must refuse. `_ID_EPISODE_MARK`/`_ID_EPISODE_RANGE` (the pack identity reading) mask
# dash markers out of a name to read its show words, which asks another question: 209 names differ in the comparison
# (`_ID_EPISODE_MARK` alone against this reading's part): 109 ranges (`_ID_EPISODE_RANGE` masks those), 58 underscore
# names (the mask runs on the raw name), 3 extensions, and the real differences: 31 code-first names (`- 13 [S04E13]`:
# the mask removes the dash, this reading takes the code), 4 dash years and 4 names with several dash markers (the mask
# takes every one). The dash marker's head and end are `_ID_EPISODE_MARK`'s (`_DASH_MARK_*`).
_DASH_MARK_HEAD = r"\s-\s*"
_DASH_MARK_END = r"(?=[\s\[\(.~_-]|$)"
_EP_CODE = re.compile(r"\b(S\d{1,2})E(\d{1,4})\b|\bEP\.?\s?(\d{1,4})\b|\bE(\d{1,4})\b|\bepisode\s?(\d{1,4})\b"
                      r"|#(\d{1,4})\b", re.I)
# A codec's or a bit depth's number written with a dash or beside `bit` (`HEVC - 10`,
# `x265 - 10 bits`, `10-bit`) is set aside before the code and dash tiers read the name, so it is never their marker.
_CODEC_DASH = re.compile(r"\b(?:hevc|avc|av1|x26[45]|h\.?26[45])\s*-\s*\d{1,2}(?:[\s-]?bits?)?(?![\w.])"
                         r"|\b\d{1,2}[\s-]?bits?\b", re.I)
_EP_DASH = re.compile(_DASH_MARK_HEAD + r"(\d{1,4})(?:v\d)?(?:\s*~\s*(\d{1,4})(?:v\d)?|-(\d{1,4})(?:v\d)?)?"
                      + _DASH_MARK_END)
_EP_AFTER_CODE = re.compile(r"(?:v\d)?(?:(?:\s*~\s*|-)(?:EP?\.?\s?)?(\d{1,4})\b|\s*[\[({]\s*(\d{1,4})\s*[\])}])", re.I)
_EP_AFTER_SEASON_CODE = re.compile(r"\s+-\s+(\d{2,4})\b")
# The absolute number written right before a season code (`067 - S02E09`, `067 (S02E09)`,
# `[067][S02E09]`), matched against the name up to the code, from the run of its separators' characters back
# (`_before_window`).
_EP_BEFORE_SEASON_CODE = re.compile(r"(?<![\w.])(\d{2,4})(?:\s+-\s+|\s*\(\s*|\s*\]\s*\[\s*)$")
_EP_BEFORE_SEASON_SEPARATORS, _EP_BEFORE_SEASON_HEAD = "-([]", 4


def _before_window(text: str, end: int, separators: str, head: int) -> int:
    """Where a pattern that ends at `end` with a run of whitespace and `separators` after a head of at most `head`
    characters can start: the run's start less `head`. Searched from there, it finds what a search from 0 finds, in
    the length of the run and not of the name (a search from 0 per marker was quadratic). The lookbehinds
    still read the text before it."""
    i = end
    while i > 0 and (text[i - 1].isspace() or text[i - 1] in separators):
        i -= 1
    return max(0, i - head)
_EP_AFTER_DASH = re.compile(r"\s*[\[({]\s*(\d{1,4})\s*[\])}]|\s*(?:&|\+|\be\b|\band\b)\s*(\d{1,4})\b", re.I)
_WORD_TOKEN = re.compile(r"[A-Za-z0-9]+")
# How a numbered title's words may be joined in a name for the cut: whitespace, `.` or `_`, or one glued mark
# (`Quill-hold`, `Lan:tern`, `Lan: tern`); never a spaced dash, which is the episode marker (`Lantern Zoku - 2`).
_TITLE_JOIN = re.compile(r"[\s._]+|[-:'’!,][\s._]*")
READ_BY_CODE, READ_BY_DASH, READ_BARE = "code", "dash", "bare"


class EpisodeReading(NamedTuple):
    """What a pack file's name says its episode is (`episode_reading`). `numbers` are the episode numbers;
    `by` the tier they came from (`code`, `dash` or `bare`); `part` the text they were read from; `stripped` the entry
    titles cut out of the name first. `codes`, for the code tier, each code's season (None for
    `E05`/`EP 05`/`Episode 05`) with the numbers it states inside that season: its own and a range's or a joined
    episode's (`S02E01-02` is `(2, {1, 2})`), not an absolute number beside it (`S02E09 - 067`, `S04E10 (69)`)."""
    numbers: frozenset
    by: str
    part: str
    stripped: tuple
    codes: tuple = ()

    def as_log(self) -> dict:
        return {"by": self.by, "part": self.part, "numbers": sorted(self.numbers), "stripped": list(self.stripped)}


def _strip_numbered_titles(text: str, titles: Iterable[str]) -> tuple[str, tuple]:
    """Cut every entry title that holds a number out of `text`, matched word for word as written (`Lantern Zoku 2`, not
    `Lantern Zoku 02`), so the title's own number is never read as the episode. A
    title whose `[A-Za-z0-9]` words are all digits is skipped (AniList's native `<kanji> 2` reads as a bare `2` and
    would cut every `2`), and a cut never spans a ` - ` (`Lantern Zoku - 2` keeps its episode): the matched words are
    joined by whitespace, `.` or `_`, or one glued mark (`_TITLE_JOIN`)."""
    tokens = [(m.group(0).lower(), m.start(), m.end()) for m in _WORD_TOKEN.finditer(text)]
    spans, stripped = [], []
    for title in titles or []:
        words = [w.lower() for w in _WORD_TOKEN.findall(str(title or ""))]
        if not any(w.isdigit() for w in words) or all(w.isdigit() for w in words):
            continue
        for i in range(len(tokens) - len(words) + 1):
            run = tokens[i:i + len(words)]
            if [w for w, _, _ in run] == words and all(
                    _TITLE_JOIN.fullmatch(text[a[2]:b[1]]) for a, b in zip(run, run[1:])):
                spans.append((run[0][1], run[-1][2]))
                if title not in stripped:
                    stripped.append(title)
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " * (end - start) + text[end:]
    return text, tuple(stripped)


def episode_reading(name: str, titles: Iterable[str] = ()) -> EpisodeReading:
    """The episode a pack file's name states. `title_matches_episode` took any number of the name, so
    `Lantern Zoku 2 - 08` was episode 2 for an entry titled `Lantern Zoku 2`, `Thornreach Season 3 - 05` episode 3 and
    `[HEVC 10 bits]` episode 10, and a pack's first such file was played.

    The name is read in this order: (1) the entry titles that hold a number are cut out as written
    (`_strip_numbered_titles`: not a title of digits alone, never across a ` - `); (2) a code (`S02E05`, `E05`,
    `EP 05`, `Episode 05`) gives the episode; else (3) the first dash marker that is not a year (` - 05`, ` - 03~04`);
    else (4) every number but the ones a pack's name states as something else (`_BARE_NOT_EPISODE`: a bit
    depth, a codec, a year, a size, `Season N`, `Vol N`; `[HEVC 10 bits]` is not episode 10). A marker keeps the
    numbers written right after it (`S04E10 (69)`, `S04E29-35`, `S02E09 - 067 - Title`, `- 10 (69)`, `- 05 & 06`), and
    a season code the absolute number right before it (`067 - S02E09`), so an absolute number or a second episode
    beside it still counts. A tier only keeps numbers that reading takes, so it can narrow a name's numbers, never add
    one."""
    text, stripped = _strip_numbered_titles(_episode_text(name), titles)
    tokens = _episode_tokens(text)
    taken = {n for n, _ in tokens}
    text = _CODEC_DASH.sub(lambda m: " " * len(m.group(0)), text)

    def marker(m: re.Match, numbers: list, followers: list,
               before: re.Pattern | None = None) -> tuple[set, str, frozenset]:
        start, end = m.start(), m.end()
        own = list(numbers)             # the marker's own numbers and a range's or joined episode's
        b = before.search(text, _before_window(text, start, _EP_BEFORE_SEASON_SEPARATORS, _EP_BEFORE_SEASON_HEAD),
                          start) if before else None
        if b and not _is_year(int(b.group(1))):
            numbers = [b.group(1)] + numbers
            start = b.start()
        for rx in followers:
            f = rx.match(text, end)
            if f:
                numbers += [g for g in f.groups() if g]
                own += [f.group(1)] if rx is _EP_AFTER_CODE and f.group(1) else []
                end = f.end()
        return ({int(g) for g in numbers if int(g) in taken}, text[start:end].strip(),
                frozenset(int(g) for g in own if int(g) in taken))

    code = [(m.group(1), *marker(m, [g for g in m.groups()[1:] if g],
                                 [_EP_AFTER_CODE] + ([_EP_AFTER_SEASON_CODE] if m.group(1) else []),
                                 _EP_BEFORE_SEASON_CODE if m.group(1) else None)) for m in _EP_CODE.finditer(text)]
    code = [(season, ns, part, own) for season, ns, part, own in code if ns]
    if code:
        return EpisodeReading(frozenset().union(*(ns for _, ns, _, _ in code)), READ_BY_CODE,
                              ", ".join(part for _, _, part, _ in code), stripped,
                              tuple((int(season[1:]) if season else None, own) for season, _, _, own in code))
    for m in _EP_DASH.finditer(text):
        if _is_year(int(m.group(1))) and not m.group(2) and not m.group(3):
            continue                    # `Show - 2014 - 05`: a year, not the marker
        dash, part, _ = marker(m, [g for g in m.groups() if g], [_EP_AFTER_DASH])
        if dash:
            return EpisodeReading(frozenset(dash), READ_BY_DASH, part, stripped)
        break
    bare = text
    for rx in _BARE_NOT_EPISODE:
        bare = rx.sub(" ", bare)
    tokens = [(n, part) for n, part in _episode_tokens(bare) if n in taken]
    return EpisodeReading(frozenset(n for n, _ in tokens), READ_BARE, ", ".join(p for _, p in tokens), stripped)


# ----------------------------------------------------------------------------- series and season match
#
# An indexer whose search is free text answers whatever names the words, so the only thing deciding which
# show a result belongs to used to be `title_matches_episode` — which checks that a number appears
# in the title, not that the release is this series or this season. Measured on 378 real free-text index items
# from 37 of this machine's own logged queries (2026-09-19): 86 of the 253
# the regex accepted were not the requested episode at all, and 32 of those were **another season
# of the right show** — resolving Pellin season 1 episode 7 returns
# `[Kumo-raws] Tabiji no Pellin 2nd Season - 07` and the regex takes it.

_SERIES_GROUP = re.compile(r"^\[[^\]]{1,30}\]\s*")
_SERIES_TAGS = re.compile(r"[\[\(][^\]\)]{0,40}[\]\)]")
# A second reading of a name's show part, asked only when the first (`series_part`) is below the bar
# (`_reread_shows`): `_` as a space (`Tabiji_no_Pellin_01`); a group tag in CJK lenticular brackets
# (`【Group】[Title][01]`), whose brackets hold tags as `[…]` do; and a name whose show is written only inside its
# tags (`[Group][Title][01]`, `[Group][中文名/Title][BDRip]`): each bracket after the group tag, each `/`, `／` or `|`
# alternative of it on its own.
_REREAD_LEAD = re.compile(r"^(?:\[[^\]]{1,30}\]|【[^】]{1,30}】)\s*")
_REREAD_CJK_TAGS = re.compile(r"【[^】]{0,40}】")
_REREAD_BRACKETS = re.compile(r"\[([^\]]{1,120})\]|【([^】]{1,120})】")
_REREAD_ALTERNATIVES = re.compile(r"\s*[/／|]\s*")
# Words a re-read show part may hold beside the entry's titles' words and numbers (`_reread_passes`), each met in a
# right name of the measured corpus (invented here): `Lantern_x_Lantern_06_2011_Full_HD_…_VOSTFR`,
# `Kite_Song_Season_2_01-12_End`, `Pellin_…_2012_TV_01-06`, `LANTERN_X_LANTERN_(2011)_-_Capitulo_61-63`.
_REREAD_EXTRA_WORDS = frozenset({"full", "hd", "vostfr", "end", "tv", "capitulo"})
# The release vocabulary a bracket beside a re-read part may hold (`_reread_vocabulary`), beyond `_SERIES_NOISE`: each
# met in the tags of a right tags-only name of the measured corpus (2026-11-08): language and script tags (`[GB]`,
# `[BIG5]`, `[AR-ENG]`, `[简繁字幕外挂]`, `[简日双语]`), container, audio and picture tags, a box's discs and volumes,
# and `[Movie]` beside a film's title. A format word (`OVA`, `SP`, `特典映像`, `特别篇`, `总集篇`) only in a pack's
# list of its contents (`[01-22TV全集+OVA+特别篇+总集篇]`): beside a title alone it names another entry.
_REREAD_TOKENS = re.compile(r"[a-z0-9]+|[^\x00-\x7f]+")
_REREAD_RANGE = re.compile(r"\d\s*[-~]\s*\d")
_REREAD_TAG_WORD = re.compile(r"\d+(?:v\d+|tv)?|\d+x\d+|bits?|hi10p|ac3|flacx?\d*|mkv|mp4|gb|big5|eng|ar|pc|psp|hdr"
                              r"|box\d*|disc|vol|fin|bdmv|movie")
_REREAD_FORMAT_WORD = re.compile(r"ova|sp")
_REREAD_CJK_TAG_CHARS = frozenset("简繁体體国日粤中三双雙语語内封字幕外挂")
_REREAD_CJK_FORMAT_CHARS = frozenset("全集特典映像别篇总")
_SERIES_NOISE = re.compile(
    r"\b(?:\d{3,4}p|4k|x26[45]|h\.?26[45]|hevc|avc|av1|aac\d?(?:\.\d)?|flac|opus|ddp?\d?(?:\.\d)?|eac3|"
    r"web-?dl|web-?rip|webrip|bd-?rip|bdrip|blu-?ray|bd|dvd|hdtv|remux|10-?bits?|8-?bits?|"
    r"dual-?audio|multi-?subs?|multiple subtitles?|multisub|batch|repack|uncensored|"
    r"cr|crunchyroll|dsnp|disney\+?|nf|netflix|amzn|amazon|bili|bilibili|hidive|adn|b-?global|"
    r"v\d|sub(?:bed|s)?|dub(?:bed)?|complete|series|weekly)\b", re.I)
_SERIES_EP = re.compile(
    r"(?:\b(?:s\d{1,2})?e\d{1,4}\b|\bep?\.?\s?\d{1,4}\b|\s-\s*\d{1,4}(?:v\d)?(?=\s|$)"
    r"|\b\d{1,4}\s*[~-]\s*\d{1,4}\b)", re.I)
# A season stated with digits: `S2`, `Season 2`, `2nd Season`, `S02E05`. `_SEASON_DIGITS` is what the show test
# (`_series_norm`) strips from both sides of a comparison.
_SEASON_DIGITS_ALT = r"s(?:eason)?\s*0*(\d{1,2})\b|(\d{1,2})(?:st|nd|rd|th)\s+season\b|\bs(\d{1,2})e\d+"
_SEASON_DIGITS = re.compile(r"\b(?:" + _SEASON_DIGITS_ALT + ")", re.I)
# The one table of season words, read by `season_number` (`_SEASON_STATED`) and by the pack identity reading
# (`_ID_ORDINALS`). `_SEASON_STATED` is `_SEASON_DIGITS` plus a season written as a word, and the word comes
# first so that the leftmost match wins: `Show Second Season 01` is season 2, not the `Season 01` that follows it
# (measured: one real release). The show test keeps `_SEASON_DIGITS`: stripping the words from the entry's title and
# not from an underscore-joined release name (`Ledger_Series_Second_Season_Arc_01-03`) dropped three right packs
# below the similarity bar when it was tried on the corpus.
_SEASON_WORDS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth")
_SEASON_STATED = re.compile(r"\b(?:(" + "|".join(_SEASON_WORDS) + r")\s+season\b|" + _SEASON_DIGITS_ALT + ")", re.I)
# `\w` rather than `a-z0-9`: stripping everything non-ASCII erases a native-script title
# completely, so `[Group] 旅路のペリン - 07` normalised to the empty string and matched nothing.
_SERIES_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)

SERIES_SIMILARITY = 0.62      # chosen on the 378 measured items, not a default


def _keep_marks(m: re.Match) -> str:
    """A run `_SERIES_PUNCT` found, as a space, but for the combining marks (Unicode `Mn`, `Mc`, `Me`)
    that lead it right after a letter. `\\w` is no mark, and `re` has no `\\p{M}`: cut there, a Thai or Devanagari word
    fell into fragments (`नारुतो` → `न र त`) and a decomposed accent split its word. A mark after punctuation or a
    digit is punctuation too (the variation selector after `❤`, a keycap's enclosing mark): kept, it would start the
    next word."""
    run, i = m.group(), 0
    if m.start() and m.string[m.start() - 1].isalpha():
        while i < len(run) and unicodedata.category(run[i]).startswith("M"):
            i += 1
    return run[:i] + (" " if i < len(run) else "")


# Unicode's Variation_Selector property (PropList.txt): a selector picks a glyph of the letter before it (an
# ideographic variant of a kanji, an emoji's text or colour form) and is no letter of a word.
_VARIATION_SELECTORS = re.compile("[\u180b-\u180d\u180f\ufe00-\ufe0f\U000e0100-\U000e01ef]")


def _lower(s: str) -> str:
    """`s` lowered and composed (NFC), so a decomposed accent is the letter it composes (`a` + U+0301
    is `á`). `İ` lowers to `i` + U+0307, which has no composed form; the dot is dropped, as Turkish and Azerbaijani
    lower it (`İstanbul`, `ISTANBUL` and `istanbul` are one word). The dot after `i` is the only mark `lower()` adds.
    A variation selector is dropped (`_VARIATION_SELECTORS`): kept after a letter, it would stay in the word."""
    s = str(s or "").lower()
    if s.isascii():
        return s
    return unicodedata.normalize("NFC", _VARIATION_SELECTORS.sub("", s.replace("i\u0307", "i")))


def _series_words(s: str) -> str:
    """`s`, already lowered (`_lower`), with punctuation runs as single spaces and the combining marks kept in their
    words."""
    return re.sub(r"\s+", " ", _SERIES_PUNCT.sub(" " if s.isascii() else _keep_marks, s)).strip()


def season_number(text: str) -> int | None:
    """The season a title names, or None when it names none. `None` is not 1: a title that says
    nothing is compatible with any season, which is what makes the comparison safe."""
    m = _SEASON_STATED.search(str(text or ""))
    if not m:
        return None
    word, *digits = m.groups()
    if word:
        return _SEASON_WORDS.index(word.lower()) + 1
    for g in digits:
        if g:
            return int(g)
    return None


def series_part(title: str) -> str:
    """The show name inside a release title, with the group tag, episode tokens, bracketed tags and
    encoding/source vocabulary removed. Composed first, so the `\\b` of the patterns below never falls
    between a letter and its decomposed accent."""
    t = _SERIES_GROUP.sub(" ", unicodedata.normalize("NFC", str(title or "")))
    for ext in VIDEO_EXT:
        if t.lower().endswith(ext):
            t = t[: -len(ext)]
            break
    t = _SERIES_EP.sub(" ", t)
    t = _SERIES_TAGS.sub(" ", t)
    t = _SERIES_NOISE.sub(" ", t)
    return _series_words(_lower(t))


def _series_norm(s: str) -> str:
    return _series_words(_SEASON_DIGITS.sub(" ", _lower(s)))


def _reread_part(title: str) -> str:
    """`series_part` with `_` read as a space and a `【…】` group tag and tags cut as `[…]` ones are."""
    t = _REREAD_LEAD.sub(" ", unicodedata.normalize("NFC", str(title or "")).replace("_", " "))
    return series_part("[g] " + _REREAD_CJK_TAGS.sub(" ", t))


def _reread_shows(title: str) -> list[tuple[str, list[tuple[str, bool]]]]:
    """The show parts of a name's second reading, each with the words beside it: `_reread_part` when it differs from
    `series_part` (nothing beside it: the part is the whole name), else, when it is empty, each bracket after the group
    tag, split at `/`, `／` and `|`, with the release vocabulary cut. Beside a bracket's part are the other alternatives
    of its bracket and every other bracket after the group tag, each `(text, another script excuses it)`: an
    alternative anywhere and a bracket before the show's may be its name in another script (`[中文名/Title]`,
    `[Grp][中文名][Title]`); a bracket after the show's may not (an arc's name, `[Title][<arc> 其一][01]`).
    Never the group tag: a group named like a title's word (`[Pellin] Kite Song - 01` for an entry whose synonym is
    `Pellin`) would pass as it."""
    first, part = series_part(title), _reread_part(title)
    if part:
        return [(part, [])] if part != first else []
    t = unicodedata.normalize("NFC", str(title or "")).replace("_", " ")
    lead = _REREAD_LEAD.match(t)
    brackets = [m.group(1) or m.group(2) or "" for m in _REREAD_BRACKETS.finditer(t[lead.end():] if lead else t)]
    out: list[tuple[str, list[tuple[str, bool]]]] = []
    for i, bracket in enumerate(brackets):
        alts = _REREAD_ALTERNATIVES.split(bracket)
        for j, alt in enumerate(alts):
            alt_part = series_part(f"[g] {alt}")
            if alt_part and alt_part not in (p for p, _ in out):
                beside = [(a, True) for k, a in enumerate(alts) if k != j]
                beside += [(b, k < i) for k, b in enumerate(brackets) if k != i]
                out.append((alt_part, beside))
    return out


def _reread_beside(text: str, other_script: bool, matched: str | None, variants: list[str]) -> bool:
    """Words beside a re-read bracket's part leave the name the entry's alone: release vocabulary or numbers
    (`_reread_vocabulary`), another of the entry's names (`_reread_passes`), or, where `other_script` allows it, words
    wholly in a script the matched title does not use (`sibling_reading`'s rule). Anything else may name another show,
    season or episode (`[Title][<arc>]`, `[Title][Recap]`, `[Show A][Show B]`), which the spaced name's franchise and
    sibling readings would see and the bracket's part alone hides."""
    scripts = _scripts(text)
    if other_script and scripts and not scripts & (_scripts(matched or "") | {"Latin"}):
        return True
    if _reread_vocabulary(text):
        return True
    part = series_part(f"[g] {text}")
    return bool(part) and _part_similarity(part, variants)[0] >= SERIES_SIMILARITY and _reread_passes(part, variants)


def _reread_vocabulary(text: str) -> bool:
    """A bracket of release vocabulary: every Latin token a tag (`_SERIES_NOISE`, `_REREAD_TAG_WORD`,
    `_REREAD_EXTRA_WORDS`) or a number, every other run of letters made of `_REREAD_CJK_TAG_CHARS`. A format word
    (`_REREAD_FORMAT_WORD`, `_REREAD_CJK_FORMAT_CHARS`) counts only in a pack's list of contents, a bracket that states
    a range or `全集` (`[01-12TV全集+OVA+特典映像]`); alone it names another entry (`[Title][OVA][01]`)."""
    t = unicodedata.normalize("NFKC", text).lower()
    contents = bool(_REREAD_RANGE.search(t)) or "全集" in t
    tokens = _REREAD_TOKENS.findall(_SERIES_NOISE.sub(" ", t))
    for tok in tokens:
        if tok.isascii():
            if not (tok in _REREAD_EXTRA_WORDS or _REREAD_TAG_WORD.fullmatch(tok)
                    or (contents and _REREAD_FORMAT_WORD.fullmatch(tok))):
                return False
        elif not all(ch in _REREAD_CJK_TAG_CHARS or (contents and ch in _REREAD_CJK_FORMAT_CHARS)
                     or not ch.isalpha() for ch in tok):
            return False
    return True


def _reread_passes(part: str, variants: list[str]) -> bool:
    """A re-read part names the entry only: every word of it is a word of one of its titles, a number, or a word of
    `_REREAD_EXTRA_WORDS`. The first reading takes a name that holds a title among other words (`Show Rising - 01` for
    `Show`); a second one that did would pass another show of the franchise or a name built on a short synonym
    (`Kite_Academy_LxL` for `LxL`, `ab_-_a_tale_of_kites_End` for `End Tale`, `Show_Rising_01` for `Show`)."""
    known = {w for v in variants or () for w in _series_norm(v).split()}
    return all(w in known or w.isdigit() or w in _REREAD_EXTRA_WORDS for w in _series_norm(part).split())


def series_similarity(title: str, variants: list[str]) -> tuple[float, str | None]:
    """Best match between a release title's show name and any known title of the target.

    `variants` is what `title_variants` already builds — AniList romaji, english and synonyms, then
    ani.zip's own titles — so an alternate name the group used is matched without anything new
    being fetched. Measured: `[AnimeQX] GO FOR BEACON (Complete Series)` matches the AniList entry
    whose romaji is `Deng Ta Qianjin X`, because the English title is in its synonyms.
    """
    best, who, _ = _best_show_part(title, variants)
    return best, who


def _part_similarity(part: str, variants: list[str]) -> tuple[float, str | None]:
    """`series_similarity` of one show part."""
    cand = _series_norm(part)
    if not cand:
        return 0.0, None
    ctok = set(cand.split())
    best, who = 0.0, None
    for v in variants or []:
        n = _series_norm(v)
        if not n:
            continue
        ratio = difflib.SequenceMatcher(None, cand, n).ratio()
        vtok = set(n.split())
        if vtok and ctok:
            # A release title carries the show name plus extra words, so plain edit distance
            # under-rates a correct match; full containment of the known title is decisive.
            cover = len(ctok & vtok) / len(vtok)
            ratio = max(ratio, cover if cover == 1.0 else ratio * 0.5 + cover * 0.5)
        if ratio > best:
            best, who = ratio, v
    return best, who


def _best_show_part(title: str, variants: list[str]) -> tuple[float, str | None, str | None]:
    """`series_similarity`'s score and title, and the re-read part that scored it (None when the first reading did).
    The second reading (`_reread_shows`) is asked only below the bar, and a part of it counts only when it names the
    entry alone (`_reread_passes`), so no name the first reading passes or scores is read otherwise."""
    best, who = _part_similarity(series_part(title), variants)
    if best >= SERIES_SIMILARITY:
        return best, who, None
    chosen = None
    for part, beside in _reread_shows(title):
        sim, name = _part_similarity(part, variants)
        if (sim >= SERIES_SIMILARITY and sim > best and _reread_passes(part, variants)
                and all(_reread_beside(text, other, name, variants) for text, other in beside)):
            best, who, chosen = sim, name, part
    return best, who, chosen


# `series_similarity` passes a name by spelling alone when its words share none with any title: the
# edit ratio of a one-word name to a one-word synonym (`Hanayomigatari` to `Kabukiyomigatari`, 0.73) clears the bar,
# and so does another franchise's word (`Kekkon Yubiwa Yomigatari`). The same letters spaced otherwise
# (`LanternxLantern`, `Kabuki Yomigatari`) or a word spelt with one more vowel (`Kaikyu`/`Kaikyuu`, 0.92) are still the
# title. Measured 2026-10-04.
OTHER_ENTRY_TAG = "other-entry rule"     # ends every spelling-only and film drop reason, so a log can pick them out
SPELLING_WORD_SIMILARITY = 0.9


def _show_words(title: str) -> list[str]:
    """The words of a name's show part (`series_part`), `_` read as a space and a dotted name's `.` too."""
    t = str(title or "").replace("_", " ")
    if t.count(".") >= 3 and " " not in t.strip():
        t = t.replace(".", " ")
    return _series_norm(series_part(t)).split()


def _glued(words: list[str], other: list[str]) -> bool:
    """A word of `other` is two or more consecutive `words` written together (`lantern x lantern` → `lanternxlantern`).
    Not `extsubs._glued_title`, which reads a subtitle file's show word against a release's title and must refuse a
    franchise stem (`kazehiki` inside `Towakazehiki` is another show there); this one only keeps a release the show test
    already passed, where a franchise pack's stem is the pack (see `spelling_reading`)."""
    whole = set(other)
    longest = max(map(len, whole), default=0)
    for i in range(len(words)):
        joined = words[i]
        for word in words[i + 1:]:
            joined += word
            if len(joined) > longest:   # no longer run can be a word of `other`; joining every run was cubic
                break
            if joined in whole:
                return True
    return False


def _fold_marks(word: str) -> str:
    """`word` with its Latin letters folded: NFKD, then the combining marks taken off. That removes
    accents (`velkará` → `velkara`, `tập` → `tap`), and NFKD also folds full width and ligatures (`ﬁ` → `fi`), so a
    fan's spelling with or without them is one word. Only a Latin letter is folded: the voicing mark of kana
    (`ガ`/`カ`) and Cyrillic `й`/`и` make other letters, not accents, and NFKD would split a Hangul syllable into its
    jamo. A letter with no decomposition stays (`ß`, `ł`, `đ`, `ø`, `æ`, `ı`).
    A word keeps its combining marks (`_series_norm`). The fold also takes off, in every script, a mark
    left after composition (NFC) whose canonical combining class is not 0 (`unicodedata.combining`): Arabic harakat and
    Hebrew points, which a typist mostly leaves out (`حَارِس` for `حارس`), a mark no Latin letter composes with (`ẹ` +
    U+0301). A mark of class 0 is a letter's vowel, not an accent, and stays: a Devanagari matra (`ा`) or a Thai vowel
    above (`ิ`), so `मलिन` and `मिलना`, `กันดาว` and `กินดาว` stay two words. Thai tone marks and vowels below, the
    nukta and the virama have a class and are folded (not handled yet). NFC keeps `バ`, a Hangul syllable and `й`
    whole, so they are not touched. The one word identity of the show test, the guard (`guard_reading`) and the
    sibling reading (`sibling_reading`)."""
    if word.isascii():
        return word
    out = []
    for ch in word:
        if unicodedata.decomposition(ch) and _letter_script(ch) == "Latin":      # else NFKD leaves `ch` as it is
            ch = unicodedata.normalize("NFKD", ch)
        out.append("".join(c for c in ch if not unicodedata.combining(c)))
    return "".join(out)


def _folded_words(text: str) -> set[str]:
    """The show words of a title (`_series_norm`), each folded (`_fold_marks`), for a word's membership."""
    return {_fold_marks(w) for w in _series_norm(text).split()}


def _unspaced_title_word(word: str) -> bool:
    """A title's word that a name may write with its marker glued after it (`霧笛の鍵守第7話`): two or
    more letters, at least one of them in a script written without spaces whose words `_series_norm` keeps whole (Han,
    Kana). There no space ever stands between the title and the marker. In a script that spaces its words, a name word
    that starts with the title's word is another entry built on the stem (`Kestrelwing` for `Kestrel`, and the same in
    Greek, Hebrew, Cyrillic, Arabic or Hangul). Letters, not characters, so a marker such as `2期` is not one. Thai,
    Lao, Khmer and Myanmar are written without spaces too, and their words survive `_series_norm`; they are not
    anchors (not handled yet: measured, a glued other entry would be kept as well)."""
    scripts = [s for s in map(_letter_script, word) if s]
    return len(scripts) >= 2 and bool({"Han", "Kana"} & set(scripts))


class SpellingReading(NamedTuple):
    """`reason`, the drop (None when kept), and `why`, the clause that kept a name whose show words
    share no word with any title ("" when a word is shared, or when dropped), so a log can say why a name stayed."""
    reason: str | None
    why: str


def spelling_reading(title: str, variants: list[str], titles=()) -> SpellingReading:
    """Whether a name that passes the show test is the show. It is not when no word of its show part is a
    word of any title (`variants`, and every other title of the entry in `titles`: AniList's synonyms past the two
    `variants` holds) and none of these keeps it:
    - two or more of a title's words written together, or the reverse (`LanternxLantern`);
    - a word spelt like a title's word (`SPELLING_WORD_SIMILARITY`: `Kaikyu`/`Kaikyuu`);
    - a word of 4+ letters that is part of a title's word: a franchise pack's `Yomigatari` for `Owariyomigatari` (the
      measured corpus' chosen pack). A subtitle name may not do this (`extsubs._glued_title`): there the stem names
      another show, here the show test has already passed the name and the stem is the franchise's pack.
    Two more keeps, each logged:
    - the words compared with their Latin letters folded (`_fold_marks`: `Velkará` for `Velkara`);
    - a word of the name that starts with a title's word written in a script without spaces (`_unspaced_title_word`):
      a CJK title with its episode or season marker glued after it (`霧笛の鍵守第7話`, `霧笛の鍵守2期`), which
      `_series_norm` cannot split. Only at the start: another entry built on the franchise's stem ends with it (`花詠語`
      for the title word `詠語`). Only this way round: a short name word inside a long title's word is the 4-letter
      clause above. And never for a word of a spaced script, Latin or other, where a name word that starts with the
      title's word is another entry built on the stem (`Kestrelwing` for `Kestrel`).
    The drop starts with `series`, the show test's own code."""
    reread = _best_show_part(title, variants)[2]
    # A name the second reading passed is asked with the words of the part it read.
    words = _series_norm(reread).split() if reread else _show_words(title)
    every = [_series_norm(v).split() for v in (*(variants or ()), *(titles or ()))]
    if not words or any(set(words) & set(vw) for vw in every):
        return SpellingReading(None, "")
    folded = [_fold_marks(w) for w in words]
    every_folded = [[_fold_marks(w) for w in vw] for vw in every]
    if (folded, every_folded) != (words, every):
        shared = next((f"{a!r} ~ {b!r}" for vw, fvw in zip(every, every_folded)
                       for a, fa in zip(words, folded) for b, fb in zip(vw, fvw) if fa == fb), None)
        if shared:
            return SpellingReading(None, f"the same word once folded: {shared}")
    for vw0, vw in zip(every, every_folded):
        accents = "folded, " if (folded, vw) != (words, vw0) else ""
        if _glued(folded, vw) or _glued(vw, folded):
            return SpellingReading(None, f"{accents}glued: {' '.join(folded)!r} is {' '.join(vw)!r} written together "
                                         f"or apart")
        alike = next((f"{a!r} ~ {b!r}" for a in folded for b in vw
                      if difflib.SequenceMatcher(None, a, b).ratio() >= SPELLING_WORD_SIMILARITY), None)
        if alike:
            return SpellingReading(None, f"{accents}spelt alike: {alike}")
        part = next((f"{a!r} in {b!r}" for a in folded for b in vw if len(a) >= 4 and a in b), None)
        if part:
            return SpellingReading(None, f"{accents}part of a title's word: {part}")
        lead = next((f"{a!r} starts with {b!r}" for a in folded for b in vw
                     if _unspaced_title_word(b) and a.startswith(b)), None)
        if lead:
            return SpellingReading(None, f"{accents}a word of the name starts with a title's unspaced word: {lead}")
    sim, who = series_similarity(title, variants)
    return SpellingReading(f"series {sim:.2f} by spelling alone: {' '.join(words)!r} shares no word with any title "
                           f"(nearest {who!r}; {OTHER_ENTRY_TAG})", "")


def spelling_only_reason(title: str, variants: list[str], titles=()) -> str | None:
    """`spelling_reading`'s drop reason, or None."""
    return spelling_reading(title, variants, titles).reason


# Words a release puts between the show name and its episode marker that name *another* show
# of the franchise ("Paper Desert", "Silver Rain"). Season/part/episode vocabulary is not a name.
_SIBLING_NOISE = re.compile(r"^(?:seasons?|parts?|eps?|episodes?|cour|v\d+|e\d+\w*|s\d+\w*|the|and|of|no|an?|to|in)$")
AMBIGUOUS_SIBLINGS = 3        # measured: a refused franchise OVA has 8, every other logged resolve 0 or 1 (2026-09-26)


def _letter_script(ch: str) -> str | None:
    """The script of one letter: "Latin" (full-width too), "Han", "Kana", "Hangul", else the first word of its
    Unicode name ("CYRILLIC", "ARABIC", "THAI"). That fallback stands in for a script and is not always one (`ª` gives
    "FEMININE"); Python has no script property. None for a digit, a mark or punctuation."""
    if not ch.isalpha():
        return None
    name = unicodedata.name(ch, "OTHER")
    if "LATIN" in name:
        return "Latin"
    if name.startswith(("CJK UNIFIED", "CJK COMPATIBILITY IDEOGRAPH", "IDEOGRAPHIC")):
        return "Han"
    if "HIRAGANA" in name or "KATAKANA" in name:
        return "Kana"
    if name.startswith("HANGUL"):
        return "Hangul"
    return name.split()[0]


def _scripts(text: str) -> set[str]:
    return {s for s in map(_letter_script, text) if s}


def sibling_names(titles: list[str], variants: list[str], movie: bool = False,
                  every_script: bool = False, own=None) -> list[str]:
    """Distinct names, other than the target's own, that series-matching release titles give their
    show. Three or more means the index answered for the franchise, not for this entry. `movie` and
    `every_script`: see `sibling_name`. `own`: the guard's reading, `guard_reading`, with every title of
    the entry."""
    if own is not None:
        return sorted({n for n in (guard_reading(t, variants, movie, own)[0] for t in titles or []) if n})
    return sorted({n for n in (sibling_name(t, variants, movie, every_script) for t in titles or []) if n})


# Words a name puts before its marker that are no show's name: "episode" and "season" in the plugin's
# other languages (`[WZF] Show - Capitulos 01~05`, `Temporada 2`, `Bölüm`, `Tập`; `_SIBLING_NOISE` reads them only in
# English; Cyrillic and Arabic forms are set aside by the script rule), and a format or an extra (`… the Movie`,
# `NCOP 01`, `Arc 01-03`, `Vol. 1`). Measured 2026-10-04. A season marker
# `_SIBLING_NOISE` does not read (`2nd`, `III`) stays a name: set aside, it turned a first season's refusal into another
# season's play (`Lantern Runner II - 04` for season 1).
# Its season words, each with the plural the film rule reads as several seasons beside a film, so the two cannot drift:
# a season word added here needs its plural (Polish and Turkish share `sezon`: `sezony`, `sezonlar`).
_SEASON_WORDS_OTHER = (("temporadas?", "temporadas"), ("saisons?", "saisons"), ("staffeln?", "staffeln"),
                       ("stagion[ei]", "stagioni"), ("sezon[ua]?", "sezony|sezonlar"))
_SIBLING_FORMAT = re.compile(r"^(?:cap[ií]tulos?|epis[oó]dios?|episodi|[eé]pisodes?|folgen?|odcinek|odcink[ai]"
                             r"|b[oö]l[uü]m|t[aậ]p|afleverings?|серии|серия"
                             r"|" + "|".join(word for word, _ in _SEASON_WORDS_OTHER) +
                             r"|movies?|films?|ovas?|oav|oad|specials?|tvsp|ncop|nced|recaps?|vol|volumes?|arc"
                             # The extras words `_EXTRA_FILE` and the pack identity's `_ID_EXTRA_WORDS` read (a
                             # `… Complete Series v2 + Specials & Extras` counted `extras` as a show).
                             r"|extras?|bonus|previews?|trailers?|creditless|menus?)$")


def guard_reading(title: str, variants: list[str], movie: bool = False, own=()) -> tuple[str | None, list[list[str]]]:
    """`(name, set_aside)` for the ambiguity check: `sibling_name`'s name without an episode or format
    word (`_SIBLING_FORMAT`) and without a word of any of the entry's own
    titles (`own`, `cli.entry_titles`: AniList's synonyms past the two `variants` holds name its arcs and subtitles,
    `Shisho ni Naru Tame ni wa…`); `set_aside` is `[word, why]` for each word left out, for the trace.
    A pack that holds the entry's own episodes beside other shows (`own_holding_pack`) names no sibling."""
    name, aside = guard_words(title, variants, movie, own)
    held = own_holding_reading(title, variants, movie, own) if name else None
    if held is not None:
        # The cut, the mark and the show test's score, so a wrongly cleared sibling can be read from the log.
        return None, [*aside, [name, f"{OWN_HOLDING_WHY} (head {held['head'].strip()!r}, mark {held['mark']!r}, "
                                     f"similarity {held['similarity']:.2f} to {held['matched']!r})"]]
    return name, aside


def guard_words(title: str, variants: list[str], movie: bool = False, own=()) -> tuple[str | None, list[list[str]]]:
    """`guard_reading` before its own-holding rule: the words a name gives besides the entry's own and the format
    words, whether or not the pack also holds the entry. The pack file pick asks it whether a pack names more than the
    entry (`multi_show_pack`)."""
    name = sibling_name(title, variants, movie)
    if not name:
        return None, []
    own_exact: set[str] = set()
    for t in own or ():
        own_exact |= set(_series_norm(t).split())
    own_words = {_fold_marks(w) for w in own_exact}   # the show test's word identity, folded
    kept, aside = [], []
    for w in name.split():
        why = ("an episode, season or format word" if _SIBLING_FORMAT.match(w) else
               "a word of the entry's own titles" if w in own_exact else
               "a word of the entry's own titles once folded" if _fold_marks(w) in own_words else None)
        if why:
            aside.append([w, why])
        else:
            kept.append(w)
    return " ".join(kept) or None, aside


# An own-holding pack: the entry's own name first, then other shows after a bundle mark. `+`/`&` outside the
# `[..]`/`【..】` tags (the bundle reading's, `_bundle_outside_tags`), or a spaced dash once the name before it states a
# pack (`[Grp] Show (Complete Series) … - Show 0 Zero - OVAs`): a bare `Show - Arc` dash is a show's own name too
# often (`[Grp] Show - 3-nen Z-gumi Arc-sensei (01-12)`: measured, it cleared real other shows).
_OWN_HOLDING_DASH = re.compile(r"\s-\s")
_OWN_HOLDING_PACK = re.compile(r"\b(?:complete|batch)\b", re.I)
# A number the head states on its own (not a year, not glued to a letter or a dot: `v2`, `S01`, `.170` are not read).
_OWN_HOLDING_NUMBER = re.compile(r"(?<![\w.])(\d{1,3})(?![\w.])")
OWN_HOLDING_WHY = ("an own-holding pack: its name before the first bundle mark is the entry's own, so the other "
                   "shows after it are bundled beside the entry's episodes")


def own_holding_cut(title: str) -> tuple[str, str] | None:
    """`(head, mark)`: the name before its first bundle mark, and the mark (`+`, `&`, or `-` for the dash). The mark
    is `+`/`&` outside its bracket tags (a tag holding an OVA word is read, as `_bundle_outside_tags` reads it), or a
    spaced dash once the name before it states a pack (`complete`, `batch`). None without one, or with nothing before
    it."""
    t = str(title or "")
    blanked = _BRACKET_TAG.sub(lambda m: m.group(0) if any(_OVA_WORD.match(w) for w in _title_tokens(m.group(0)))
                               else " " * len(m.group(0)), t)
    marks = [(m.start(), m.group(0)) for m in (_FILM_BUNDLE.search(blanked),) if m]
    marks += [(m.start(), "-") for m in _OWN_HOLDING_DASH.finditer(blanked)
              if _OWN_HOLDING_PACK.search(t[: m.start()])][:1]
    cut, mark = min(marks, default=(None, None))
    return (t[:cut], mark) if cut is not None and t[:cut].strip() else None


def own_holding_head(title: str) -> str | None:
    """The name before its first bundle mark (`own_holding_cut`), else None."""
    cut = own_holding_cut(title)
    return cut[0] if cut else None


def own_holding_pack(title: str, variants: list[str], movie: bool = False, own=()) -> str | None:
    """The head of an own-holding pack (`own_holding_reading`), else None."""
    held = own_holding_reading(title, variants, movie, own)
    return held["head"] if held else None


def own_holding_reading(title: str, variants: list[str], movie: bool = False, own=()) -> dict | None:
    """How a pack that holds the entry's own episodes beside other shows reads, else None: its head (`own_holding_cut`)
    gives no guard name (`guard_words`), passes the show test, and states no number none of the entry's titles
    (`variants`, `own`) holds. `[G] Show Complete Series + Specials & Extras` and `[G] Show (Complete Series) - Show
    Zero - OVAs` are the entry's; `[Grp] Show (2023) (Season 4 Part 03+04) … Kouhen` states `4` and `03`, which the
    entry's titles do not, and stays a sibling, as does `Show Zero (Complete Series) - OVA`, whose head is another's.
    The reading is `{"head", "mark", "similarity", "matched"}`: the cut, its mark, and the show test's score with the
    entry title it matched (`series_similarity`)."""
    cut = own_holding_cut(title)
    if cut is None or guard_words(cut[0], variants, movie, own)[0] is not None:
        return None
    head, mark = cut
    similarity, matched = series_similarity(head, variants)
    if similarity < SERIES_SIMILARITY:
        return None
    titles = (*(variants or ()), *(own or ()))
    held = {int(n) for t in titles for n in re.findall(r"\d{1,3}", str(t))}
    said = {int(n) for n in _OWN_HOLDING_NUMBER.findall(_BRACKET_TAG.sub(" ", head))}
    return None if said - held else {"head": head, "mark": mark, "similarity": similarity, "matched": matched}


def sibling_name(title: str, variants: list[str], movie: bool = False, every_script: bool = False) -> str | None:
    """The name one release title gives its show when that is not the target's own, else None.
    The release picker labels each rejected release with it ("another show: Paper Desert").
    `movie`: the target is a movie, and a title without an episode marker names no show.
    A word written in a script the matched title does not use is not read, so a bilingual
    name's translation of the show (`星风旅人 / Hoshikaze no Tabibito - 01`) is not another show.
    `every_script` reads those words too, for the trace only. See `sibling_reading`."""
    reading = sibling_reading(title, variants, movie)
    return reading[1] if every_script else reading[0]


def sibling_reading(title: str, variants: list[str], movie: bool = False) -> tuple[str | None, str | None, list[str]]:
    """`(name, name read in every script, the scripts it was read in)` for one release title, from one
    pass. The trace logs the second and third next to the first, so a word the script rule set aside, and why,
    can be read from the log."""
    if _best_show_part(title, variants)[2] is not None:
        # Passed only by the second reading, whose part names the entry alone (`_reread_passes`, `_reread_beside`):
        # what is left beside it is release vocabulary (`Full HD … VOSTFR`, `BOX Disc 1-5`, `Vol.1-8`), no show.
        return None, None, []
    vtok: set[str] = set()
    for v in variants or []:
        vtok |= _folded_words(v)                      # the show test's word identity, folded
    # Old fansub file names separate words with `_` (`[QG]_Lantern_x_Lantern_010_`), which
    # `_series_norm` keeps as one word no known title contains — a false sibling per naming style.
    title = str(title or "").replace("_", " ")
    similarity, matched = series_similarity(title, variants)
    if similarity < SERIES_SIMILARITY:
        return None, None, []             # does not claim to be this show at all (a bad search)
    t = _SERIES_GROUP.sub(" ", title)
    m = _SERIES_EP.search(t)
    if movie and not m:
        # A movie's release has no episode marker, so what follows its name is language, group
        # and format words ("English", "VOSTFR", "MULTi-BRAMBLE"), not another show. A marked title
        # still names one: an index answering a movie's id with the franchise's episodes still refuses.
        return None, None, []
    words = _series_norm(series_part(t[: m.start()] if m else t)).split()
    # Chinese uploaders name a release in Chinese and romaji (`【Group】★07月新番★[星风旅人 / Romaji]`).
    # The romaji matched the show, and the Chinese words are its translations, not other shows: three of them refused
    # a show whose own releases were found. A word in a script the matched title does not use is not read; Han and
    # kana are separate scripts. The matched title, not every variant: a native Japanese title's kanji would admit
    # every Chinese word. Measured 2026-09-29: over 1189 replayed
    # resolves only the two refused shows change, and a franchise answer still refuses on its Latin words
    # (`玻璃港 glass harbor`).
    # Latin is always read: a release matched through a native title (`星風の旅人 Glass Harbor - 01`)
    # still names its sibling in romaji, and without Latin a franchise answered that way would never refuse.
    known = _scripts(matched or "") | {"Latin"}
    words = [w for w in words if _fold_marks(w) not in vtok and not w.isdigit() and len(w) > 2
             and not _SIBLING_NOISE.match(w)]
    name = " ".join(w for w in words if not _scripts(w) - known)
    return name or None, " ".join(words) or None, sorted(known)


# The episode markers of a release name, for a movie target. A movie's own release names no episode, so a
# name that does is a TV episode or pack of another show carrying the movie's words (`Other Show S02E19 Your Real
# Title`, `Other Show - S01E01 - Title`). Measured 2026-09-29.
# Not `_SERIES_EP`: it misses `Episode 01` and ` - 08[`, and `sibling_name` reads it for every show. Nor
# `_SERIES_GROUP`, `_BATCH`/`_PACK_RANGE` or extsubs' `_YEAR`: the measured rule (0 differences) is
# these exact patterns, and the shared ones decide other verdicts for every show.
# `NxNN` needs no `.`/digit before it and no `.digit` after (`AAC2.0x2`, `OPUS 2x2.0` are audio), and a
# dash number followed by `.digit` is a decimal (`- 2.0`, `- 3.0+1.01`), not an episode.
_LEAD_GROUPS = re.compile(r"^\s*(?:\[[^\]]{1,40}\]\s*)+")
_MARK_PAIR = re.compile(r"(?:\b|(?<=\d))S(\d{1,2})\s?E\s?(\d{1,4})\b"
                        r"|(?<![.\d])\b(\d{1,2})x(\d{1,4})\b(?!\.\d)", re.I)
_MARK_DASH = re.compile(r"\s-\s*(\d{1,4})(?:v\d)?(?=\s|$|[\[(]|\.(?!\d))")
_MARK_EP = re.compile(r"\b(?:ep(?:isode)?\.?\s?|e)(\d{1,4})\b", re.I)
_MARK_RANGE = re.compile(r"(?<![\w.])(\d{1,4})\s*[~-]\s*(\d{1,4})(?![\w.])")


def _is_year(n: int) -> bool:
    return 1900 <= n <= 2099


def movie_episode_markers(title: str, own_pairs=frozenset()) -> list[str]:
    """The episodes a release name states, as a movie target reads it: `SxxEyy`/`NxNN`, ` - NN`,
    `EpNN`/`Episode NN`/`ENN`, and an episode range (`01-03`, `Batch 1-26`), after the leading `[Group]` tags.
    Not markers: the movie's own ani.zip season-0 pair (`own_pairs`, `anizip.movie_pairs`: a movie filed as
    `S00E10` is named so), episode 0 (`[Grp] Title - 00`: some groups number the movie so), a dash before a year
    (`Title - 2016`) and a year range (`(2004-2019)`). Empty for a name that states no episode."""
    t = _name_body(title).replace("_", " ")
    found: list[str] = []
    spans = []
    for m in _MARK_PAIR.finditer(t):
        se = (int(m.group(1) or m.group(3)), int(m.group(2) or m.group(4)))
        spans.append(m.span())
        if se not in own_pairs:
            found.append(f"S{se[0]:02d}E{se[1]:02d}")
    for rx, label in ((_MARK_DASH, "- {}"), (_MARK_EP, "E{}")):
        for m in rx.finditer(t):
            n = int(m.group(1))
            if any(a <= m.start() < b for a, b in spans) or n == 0 or (rx is _MARK_DASH and _is_year(n)):
                continue
            found.append(label.format(m.group(1)))
    for m in _MARK_RANGE.finditer(t):
        a, b = int(m.group(1)), int(m.group(2))
        if a < b and not _is_year(a):
            found.append(f"{m.group(1)}-{m.group(2)}")
    return found


def movie_episode_reason(title: str, own_pairs=frozenset()) -> str | None:
    """`"movie_episode S02E19"` (the code, then at most three markers) for a name a movie drops, else None.
    The one reason string every movie filter returns, so a caller reads its code with `why.split()[0]`."""
    marks = movie_episode_markers(title, own_pairs)
    return f"movie_episode {', '.join(marks[:3])}" if marks else None


# A one-episode entry (any non-movie entry whose AniList `episodes` is 1: most are specials, OVAs and
# ONAs) is asked for episode 1, and its own releases rarely say 1: `S04E30`, `Special 2`, or no number. These read
# what such a name says instead. They are the exact patterns measured on
# 2026-09-30.
# `OVA N`/`OAD N` number specials too (a franchise that files each OVA as its own entry).
_SPECIAL_NUMBER = re.compile(r"(?<![^\W_])(?:specials?|sp|ova|oad)[\s._-]*0*(\d{1,2})(?![^\W_])", re.I)
_HALVES = (("zenpen", re.compile(r"(?<![^\W_])zenpen(?![^\W_])|前編", re.I)),
           ("kouhen", re.compile(r"(?<![^\W_])k(?:ou|o|ō)hen(?![^\W_])|後編", re.I)))
_WORD_MOVIE = re.compile(r"\b(?:the\s+)?(?:movie|film)\b|\bgekijouban\b|\beiga\b|劇場版|剧场版", re.I)
# A name that calls itself a special, which a related show's episode does not (measured on 2026-09-30).
_SPECIAL_WORD = re.compile(r"(?<![^\W_])(?:ova|oad|specials?|sp|tvsp)(?![^\W_])", re.I)
# AniList's disambiguator on a related show's title (`School Days (TV)`, `Hiyokoi (2012)`), which no group name
# carries; measured in the same 2026-09-30 measurement.
# Starts only where a run of whitespace starts: from inside one it was retried from every space.
_DISAMBIGUATOR = re.compile(r"(?<!\s)\s*\((?:tv|\d{4})\)\s*$", re.I)
_MARK_NUMBER = re.compile(r"(?:S\d+E|- |E)(\d+)")


def _title_tokens(text: str, as_written: bool = False) -> list[str]:
    """Lower-case words and numbers, apostrophes joined, punctuation (dots too) a separator, movie words left out
    (kept `as_written`)."""
    s = str(text or "").replace("_", " ").replace("×", "x")
    s = s if as_written else _WORD_MOVIE.sub(" ", s)
    s = s.lower().replace("'", "").replace("’", "")
    return re.sub(r"[^\w\s]+", " ", s).split()


def _token_numbers(tokens) -> set[int]:
    """The numbers among `_title_tokens`. `isdecimal`, not `isdigit`: `²` or `②` is a digit `int()` cannot read; and
    at most `NUMBER_DIGITS_MAX` digits: `int()` refuses over 4,300, which ended a movie's search; the longest
    number of a recorded name has 8."""
    return {int(w) for w in tokens if w.isdecimal() and len(w) <= NUMBER_DIGITS_MAX}


def _title_words(text: str) -> list[str]:
    """`_title_tokens` without the bare numbers."""
    return [w for w in _title_tokens(text) if not w.isdigit()]


def _name_body(title: str) -> str:
    """The name without its leading `[Group]` tags and its video extension (`VIDEO_EXT`): what the movie rule
    and the one-episode rule both read."""
    t = _LEAD_GROUPS.sub(" ", str(title or ""))
    low = t.lower()
    return next((t[: -len(ext)] for ext in VIDEO_EXT if low.endswith(ext)), t)


COMPILATION_FILM = "compilation_film"
# A bundle names its parts joined by `+`/`&` (`Show S1+S2+SP+Movie`, `[Grp][Show&After Story&The MOVIE]`, inside a
# bracket too) and states no range `parse_title` reads: the film is one part of it, beside the episodes.
_FILM_BUNDLE = re.compile(r"[+&]")
# A bundle joined by words or commas (`Show (TV and Movie)`, `Show S1-S2 and Movie`, `Complete Collection
# (TV, OVA, Movie)`), read by the episodes' part it names beside the film rather than by its joiner, which each language
# writes in its own words. A part is a tag every language writes (`TV`, an OVA word `_OVA_WORD`, a season range `S1-S2`)
# or a word from a list (`_FILM_PART_LISTED`): the `series` root most Latin-script languages share, and several seasons,
# English's `Seasons` and the plurals of the guard's season words (`_SEASON_WORDS_OTHER`). It counts only in the film
# word's own bracket group (the name outside every bracket is one group), so a channel or rip tag in a group of its own
# (`(Fuji TV)`, `[TV Rip]`) is no part, and `TV` right before the film word names a TV film (`TV Movie`, `TV.Movie`; a
# spaced dash is a separator, `TV - Movie`, as `,` and `/` are). Not one season (`Show Season 4 - Eternal Movie 2` is a
# film), not an episode range (`S01-12`), not a separator alone and not `Complete`: film cuts write each (`Show Movie:
# Driftwood, Multi-Audio, Multi-Subs`, `Show Movie 3 … Complete subbed only`). A listed word can be the entry's own
# (`Show Series Movie Cuts` for `Show Series: Second Season`) and is then no part; a tag never is (`School Days (TV)`).
# A hyphen joins a word of the name (`TV-Rip` is no `TV`). Measured 2026-10-17 over the corpus's 3,838 names, the
# capture and the 75/25 runs; the OVA rule's `_FILM_BUNDLE` reading is unchanged.
_FILM_PART_TAG_WORD = re.compile(r"^(?:tv|s\d{1,2}-s\d{1,2})$")
_FILM_PART_LISTED = re.compile(r"^(?:s[eé]ries?|seasons|" + "|".join(p for _, p in _SEASON_WORDS_OTHER) + r")$")
_FILM_TOKEN = re.compile(r"[^\W_]+(?:-[^\W_]+)*")
_FILM_GROUP_OPEN, _FILM_GROUP_CLOSE = "([{【「（［｛『〔", ")]}】」）］｝』〕"
# Ends a keep's `why` and nothing else: a grep for "(film-part rule)" counts keeps.
FILM_PART_TAG = "film-part rule"


class FilmPart(NamedTuple):
    """`word`, the part a name sets beside its film (None when none), and `own`, each listed word found there
    but set aside as a word of the entry's own titles, for the drop's reason."""
    word: str | None
    own: list[str]


def _film_groups(title: str) -> list[str]:
    """A name's bracket groups, each the text directly inside one bracket pair (`_FILM_GROUP_OPEN`: `()[]{}`,
    `【】「」` and the full-width `（）［］｛｝『』〔〕`), and the text outside every bracket as one more. An unclosed bracket
    runs to the end; a closer with nothing open stays text (`the Movie :)`)."""
    groups, stack = [""], [0]
    for ch in title:
        if ch in _FILM_GROUP_OPEN:
            groups.append("")
            stack.append(len(groups) - 1)
        elif ch in _FILM_GROUP_CLOSE and len(stack) > 1:
            stack.pop()
        else:
            groups[stack[-1]] += ch
    return groups


def _film_part(title: str, titles=()) -> FilmPart:
    """The first word of a name that names an episodes' part in a bracket group that holds a film word
    (`_film_groups`): a tag (`_FILM_PART_TAG_WORD`, `_OVA_WORD`; not a `TV` right before the film word), or a listed
    word (`_FILM_PART_LISTED`) that no title of the entry holds. The entry's words are read as the OVA rule reads them
    (`_title_tokens`, AniList's `(TV)`/`(2012)` cut off); both sides are read composed (NFC: a decomposed `série`)."""
    own_words = {w for t in titles or ()
                 for w in _title_tokens(_DISAMBIGUATOR.sub("", unicodedata.normalize("NFC", str(t))))}
    own: list[str] = []
    for group in _film_groups(unicodedata.normalize("NFC", str(title or "")).lower()):
        if not _WORD_MOVIE.search(group):
            continue
        for m in _FILM_TOKEN.finditer(group):
            w = m.group(0)
            if _FILM_PART_TAG_WORD.match(w) or _OVA_WORD.match(w):
                if w == "tv" and _WORD_MOVIE.match(group[m.end():].lstrip(" ._")):
                    continue
                return FilmPart(w, own)
            if _FILM_PART_LISTED.match(w):
                if w not in own_words:
                    return FilmPart(w, own)
                own.append(w)
    return FilmPart(None, own)


class FilmReading(NamedTuple):
    """`reason`, the drop (None when kept), and `why`, why a name that names a film was kept ("" when it
    names none or is dropped), so a log can answer "why wasn't this film dropped?"."""
    reason: str | None
    why: str


def compilation_film_reading(title: str, episode: int, alt=(), variants=()) -> FilmReading:
    """For a TV entry's episode (not a movie, not a one-episode entry: the caller decides), whether a
    release is a film cut of the show and not the episode: it is not a pack, nor a bundle that names an episodes' part
    beside the film (`_film_part`: `Show (TV and Movie)`), it names a film (`_WORD_MOVIE`), no number
    of it is the episode or an `alt` number (`title_matches_episode`), and no title of the entry names a film.
    `Nekoyomigatari (White) the Movie (BD 1080p)`, which an episode-id search files under the first episode of the arc
    it compiles."""
    title = str(title or "")
    m = _WORD_MOVIE.search(title)
    if not m:
        return FilmReading(None, "")
    if parse_title(title)["batch"] or _FILM_BUNDLE.search(title):
        return FilmReading(None, "a pack: the film beside the episodes")
    part = _film_part(title, variants)
    if part.word:
        return FilmReading(None, f"a bundle: the film beside {part.word!r} ({FILM_PART_TAG})")
    if title_matches_episode(title, episode, alt):
        return FilmReading(None, f"states episode {episode}")
    own = next((str(v) for v in variants or () if _WORD_MOVIE.search(str(v))), None)
    if own:
        return FilmReading(None, f"the entry's title {own!r} names a film")
    own_part = (f"; {', '.join(map(repr, part.own))} beside the film is a word of the entry's own titles, not a "
                f"bundle's part") if part.own else ""
    return FilmReading(f"{COMPILATION_FILM} names a film ({m.group(0)!r}) and states no episode {episode}: a film cut "
                       f"of the show is not a TV entry's episode ({OTHER_ENTRY_TAG}){own_part}", "")


def _special_reading(text: str) -> tuple[set[int], set[str]]:
    """The `Special N`/`SP N`/`OVA N`/`OAD N` numbers and the halves (`zenpen`, `kouhen`) a text names."""
    return ({int(m.group(1)) for m in _SPECIAL_NUMBER.finditer(text)},
            {half for half, rx in _HALVES if rx.search(text)})


NO_TITLE_IN_FULL = "single_episode names no title of the entry in full"      # the one-episode rule's commonest drop


def single_episode_reason(title: str, variants: list[str], related=(), alt=()) -> str | None:
    """`"single_episode <why>"` for a name a one-episode entry drops, else None. Checked on a name that is
    not a pack and that the show test keeps. It names another special (`Special 1` for the entry whose titles say
    `Special 2`), the other half (`Zenpen` for a `Kouhen` entry), or none of the entry's titles in full (every word of
    one title, in any order: the series test alone keeps `Tidewater Lantern S05` for a Tidewater Lantern special). A
    number or half is compared only when the entry's own titles state one.

    Or it is an episode of a related show (`related`: `(id, title, format)` from
    `anilist.related_shows`), read by `related_show_reason` with the query's `alt` numbers."""
    body = _name_body(title)
    own_numbers, own_halves = set(), set()
    for v in variants or []:
        n, h = _special_reading(str(v))
        own_numbers |= n
        own_halves |= h
    numbers, halves = _special_reading(body)
    if own_numbers and numbers and not numbers & own_numbers:
        return (f"single_episode names special number {', '.join(map(str, sorted(numbers)))}, the entry is number "
                f"{', '.join(map(str, sorted(own_numbers)))}")
    if own_halves and halves and not halves & own_halves:
        return f"single_episode names {', '.join(sorted(halves))}, the entry is {', '.join(sorted(own_halves))}"
    words = set(_title_words(body))
    matched = [vw for vw in (_title_words(v) for v in variants or []) if vw and set(vw) <= words]
    if not matched:
        return NO_TITLE_IN_FULL
    return related_show_reason(title, matched, related, alt)


def related_show_reason(title: str, matched: list[list[str]], related=(), alt=()) -> str | None:
    """`"single_episode names related <id> (<title>) with <marker>, …"` when a name the one-episode
    entry's titles let in (`matched`: the words of each title it holds in full) is an episode of a related show with
    more than one episode (`related`: `(id, title, format)`). The measured rule: the name states an episode
    marker (`movie_episode_markers`) other than `S00Exx`, where groups file a special; it holds every word and number
    of one of that show's titles; and every entry title it matched is inside that show's titles taken together (so
    `Tidewater Lantern OVA - 01` is kept against `Tidewater Lantern: Harbor Lights - 01`). A name that calls itself
    `OVA`/`OAD`/`Special`/`SP`/`TVSP` is kept, unless that show is itself an OVA or SPECIAL.

    A marker whose number is one of the query's `alt` numbers is the entry's own (ani.zip
    files a special as the series' absolute 13: `- 13`); episode 1 is never one. And a related title is read without
    a trailing `(TV)` or `(YYYY)` when the name must hold it in full."""
    if not related:
        return None
    own = {int(n) for n in alt or ()}
    marks = [m for m in movie_episode_markers(title) if not m.startswith("S00E") and not (
        (n := _MARK_NUMBER.fullmatch(m)) and int(n.group(1)) in own)]
    if not marks:
        return None
    body = _name_body(title)
    tokens = _title_tokens(body)
    words, numbers = {w for w in tokens if not w.isdecimal()}, _token_numbers(tokens)
    special = bool(_SPECIAL_WORD.search(body))
    union: dict = {}
    for rid, rt, _ in related:
        union.setdefault(rid, set()).update(_title_tokens(rt))
    for rid, rt, fmt in related:
        if special and fmt not in ("OVA", "SPECIAL"):
            continue
        rtoks = _title_tokens(_DISAMBIGUATOR.sub("", rt))
        rwords = {w for w in rtoks if not w.isdecimal()}
        if (rwords and rwords <= words and _token_numbers(rtoks) <= numbers
                and all(set(vw) <= union[rid] for vw in matched)):
            return f"single_episode names related {rid} ({rt}) with {marks[0]}, and no more of the entry's titles"
    return None


RELATED_ENTRY_TAG = "related-entry rule"  # ends every `related_entry_reading` drop, so a log can pick them out
# `AAC 2.0`, `5.1`: no number of a title (`Part 2`). From a run's first digit: from every digit of a long run it was
# quadratic (5 s for 50,000 digits).
_DECIMAL = re.compile(r"(?<!\d)\d+\.\d+")


class RelatedEntryReading(NamedTuple):
    """`reason`, the drop (None when kept), and `why`, why a name holding every word of a related entry's
    title was kept ("" when it holds none), so a log can answer "why wasn't this dropped?"."""
    reason: str | None
    why: str


@functools.lru_cache(maxsize=16)
def _related_readings(related: tuple) -> tuple:
    """Each related title read once per answer, not once per release: `(rows,
    union)`, a row `(id, title, tokens, words, numbers)` per title (AniList's `(TV)`/`(YYYY)` cut, read as written) and
    each entry's tokens taken together."""
    rows, union = [], {}
    for rid, rt, *_ in related:
        rtoks = _title_tokens(_DISAMBIGUATOR.sub("", str(rt)), as_written=True)
        union.setdefault(rid, set()).update(rtoks)
        rows.append((rid, rt, tuple(rtoks), frozenset(w for w in rtoks if not w.isdecimal()),
                     frozenset(_token_numbers(rtoks))))
    # Shared by every caller through the cache, so nothing in it can be changed.
    return tuple(rows), types.MappingProxyType({rid: frozenset(t) for rid, t in union.items()})


def related_entry_reading(title: str, titles=(), related=(), own_pairs=frozenset()) -> RelatedEntryReading:
    """For a movie, whether a release is another entry of its franchise (`related`: `(id, title, format, episodes)` from
    `anilist.franchise_shows`) named without an episode marker. The movie's episode markers drop a marked one and the
    related-show rule is a one-episode entry's; nothing else read an unmarked name past the show test, which a generic
    synonym of the movie (`Harbor Song Movie`) lets in for every film of the franchise. Dropped when, for one related
    entry:
    1. the name holds every word and number of one of its titles (AniList's `(TV)`/`(YYYY)` cut, a decimal's numbers
       not read: `AAC 2.0` is no `Part 2`);
    2. it holds one or more of the movie's titles (`titles`) in full;
    3. that entry's title adds a word or number to them (`Lantern Road`): a related title they equal (AniList lists
       the movie's own titles as a commercial's synonyms) names nothing more specific than the movie. A title that adds
       only a number does not count when the name states a held title of the movie with that title's own numbers,
       besides the related title's (`Harbor Song Movie 2 [10-bit]` against `Harbor Song Movie 10`: the `10` is a
       codec's);
    4. every word of the movie's titles the name holds is one of that entry's titles' words: it matched the movie only
       through titles inside that entry's (`Tidewater Dawn` beside it keeps a bundle of both films); and
    5. it states no episode marker (`movie_episode_markers`), which the movie's episode-marker rule reads.
    Every text is read as written, film words included and no word list asked: a TV series' title is the movie's
    `… Movie` title without its film word, so a name holding that title is the movie's and kept. Measured
    on 2026-10-17: over 1,337 names of the 12 movies the rule drops 6 other-entry names and no own release."""
    if not related:
        return RelatedEntryReading(None, "")
    tokens = _title_tokens(_DECIMAL.sub(" ", _name_body(title)), as_written=True)
    words, numbers = {w for w in tokens if not w.isdecimal()}, _token_numbers(tokens)
    own = []
    for t in titles or ():
        toks = _title_tokens(t, as_written=True)
        own.append((str(t), {w for w in toks if not w.isdecimal()}, _token_numbers(toks)))
    matched = [(t, ws, ns) for t, ws, ns in own if ws and ws <= words]
    mwords = {w for _, ws, _ in matched for w in ws}
    held = words & {w for _, ws, _ in own for w in ws}
    rows, union = _related_readings(tuple(tuple(r) for r in related))
    why = ""
    for rid, rt, rtoks, rwords, rnums in rows:
        if not rwords or not rwords <= words or not rnums <= numbers:
            continue
        extra = set(rtoks) - mwords
        left = sorted(held - union[rid])
        # A related title adding only a number is set aside when the name
        # states a held title of the movie with its own numbers besides that title's (`Film 10 2` is no `Film 10`).
        stated = None if rwords - mwords else next((t for t, _, ns in matched if ns and ns <= numbers - rnums), None)
        kept = (f"holds related {rid} ({rt}) and no title of the movie in full" if not matched else
                f"related {rid}'s title {rt!r} adds no word to the movie's title {matched[0][0]!r}" if not extra else
                f"related {rid}'s title {rt!r} adds only a number to the movie's title {stated!r}, whose own number "
                f"the name states besides that title's" if stated else
                f"holds related {rid} ({rt}) and the movie's words {left[:5]}, which that entry's titles lack"
                if left else "")
        if kept:
            why = why or kept
            continue
        marks = movie_episode_markers(title, own_pairs)
        if marks:
            return RelatedEntryReading(None, f"holds related {rid} ({rt}) and states {marks[0]}: the movie's "
                                             f"episode-marker rule reads it")
        return RelatedEntryReading(f"series names related {rid} ({rt}) and matched the movie only through "
                                   f"{matched[0][0]!r}, a title inside that entry's ({RELATED_ENTRY_TAG})", "")
    return RelatedEntryReading(None, why)


# A later-season entry's single whose show words are exactly a related show's title. The measured rule, two relation
# hops (2026-10-02): the name's words before its episode marker. Leading tags may be `[..]`, `(..)` or `【..】`; a dotted
# scene name's `.` and every `_` read as spaces. The marker is the first explicit one whose number is the episode or an
# alt number, else the last bare number that is: a title's own trailing number (`Show 2 Episode 02`) is never it. The
# explicit markers are the movie rule's (`_MARK_PAIR`, `_MARK_DASH`, `_MARK_EP`, so `- 2.0` is a decimal here too) plus
# `#NN`; a pair's season (`S04`, the `2` of `2x05`) stays among the words, since a name that states its season is
# marked. Not `extsubs._show_head`: it cuts at the first bare number (`Show 2 Episode 02` would read as `Show`) and its
# `_show_words` drop years (`Show (2014) - 01`, a second cour's mark, would read as `Show`). The bare-number fallback
# takes only a number `episode_reading` reads as the episode (not a `10 bits`, a `Season 1`, a `Vol. 1`): measured, the
# same 99 drops over the measured sweep, only the kept names' logged words move.
_FRANCHISE_LEAD = re.compile(r"^\s*(?:(?:\[[^\]]{0,60}\]|\([^)]{0,40}\)|【[^】]{0,60}】)\s*)+")
_FRANCHISE_MARK = re.compile("|".join(f"(?:{rx.pattern})" for rx in (_MARK_PAIR, _MARK_DASH, _MARK_EP))
                             + r"|#(\d{1,4})\b", re.I)
_FRANCHISE_EPISODE, _FRANCHISE_SEASON = (2, 4, 5, 6, 7), (1, 3)       # its groups: the episode, a pair's season
FRANCHISE_SHOW = "franchise_show"


def franchise_words(title: str, episode: int, alt=()) -> tuple[frozenset, int | None, bool]:
    """`(words, number, explicit)`: the tokens (`_title_tokens`, as written: a name's `(2014)` is how a group
    marks a cour, so no disambiguator is dropped) of the name before the marker that states `episode` or an `alt`
    number (`_franchise_span`), that number, and whether the marker was explicit. `(frozenset(), None, False)` when no
    number is."""
    span, n, explicit = _franchise_span(title, episode, alt)
    return (frozenset(), None, False) if span is None else (frozenset(_title_tokens(span)), n, explicit)


_TAGS_EPISODE = re.compile(r"\d{1,4}(?:v\d)?")


@functools.lru_cache(maxsize=4096)
def _tags_opened(title: str) -> str:
    """A name written wholly in tags (`[Grp][Title][01][1080p]`: nothing is left outside them once the second
    reading cuts its group tag and tags, `_reread_part`) as its spaced twin is written: its brackets after the group
    tag opened from the first that is not release vocabulary (`_reread_vocabulary`) on, up to the first opened one
    that is a bare episode number, written as the twin's marker, `[Grp] Title - 01 [1080p]`; the brackets after it
    stay tags, as the twin's are (`[Grp][Title][01][x264 01]` read with its tag as words would end at the tag's `01`).
    Read as leading tags, such a name had no words before its episode marker, so the franchise rule and every reader
    of its text passed what the second reading passed, a later season's `[Grp][<the first season's title>][01]` among
    them, which its twin `[Grp] <title> - 01` is refused as. Leading vocabulary stays a tag (`[Grp][BIG5][Title][01]`);
    any other name is returned as given."""
    t = unicodedata.normalize("NFC", str(title or "")).replace("_", " ")
    lead = _REREAD_LEAD.match(t)
    if lead is None or _reread_part(t):
        return title
    opened, marked = False, False

    def bracket(m: re.Match) -> str:
        nonlocal opened, marked
        inner = m.group(1) or m.group(2) or ""
        if marked:
            return m.group(0)
        opened = opened or not _reread_vocabulary(inner)
        if opened and _TAGS_EPISODE.fullmatch(inner.strip()):
            marked = True
            return f" - {inner.strip()} "
        return f" {inner} " if opened else m.group(0)

    return t[:lead.end()] + _REREAD_BRACKETS.sub(bracket, t[lead.end():])


def _franchise_text(title: str) -> str:
    """The text `_franchise_span` reads its marker in: the name without its leading tags and extension, a dotted scene
    name's dots and every `_` read as spaces. A name written wholly in tags is read as its spaced twin
    (`_tags_opened`)."""
    t = _FRANCHISE_LEAD.sub(" ", _name_body(_tags_opened(title)))
    if t.count(".") >= 3 and " " not in t.strip():
        t = t.replace(".", " ")
    return t.replace("_", " ")


def _franchise_span(title: str, episode: int, alt=()) -> tuple[str | None, int | None, bool]:
    """`franchise_words`' reading: `(text, number, explicit)`, the name's text before the marker that states `episode`
    or an `alt` number (its leading tags, extension and `_` set aside), or `(None, None, False)` when no marker does."""
    t = _franchise_text(title)
    want = {int(episode), *(int(n) for n in alt or ())}
    for m in _FRANCHISE_MARK.finditer(t):
        n = int(next(m.group(g) for g in _FRANCHISE_EPISODE if m.group(g) is not None))
        if n in want:
            cut = next((m.end(g) for g in _FRANCHISE_SEASON if m.group(g) is not None), m.start())
            return t[:cut], n, True
    last, read = None, episode_reading(t).numbers
    for m in _EP_TOKENS.finditer(t):
        if int(next(g for g in m.groups() if g is not None)) in want & read:
            last = m
    if last is None:
        return None, None, False
    return t[:last.start()], int(next(g for g in last.groups() if g is not None)), False


@functools.lru_cache(maxsize=4096)
def _franchise_title(text: str) -> frozenset:
    """A title's tokens without a trailing `(TV)`/`(YYYY)`, once per title: a resolve asks them of every name it reads
    (measured on the franchise corpus: 1.15 ms a name against ~1,000 related titles, uncached)."""
    return frozenset(_title_tokens(_DISAMBIGUATOR.sub("", text)))


# Why a first-season entry keeps a name that is exactly another franchise show's title: it adds no word none of the
# entry's titles hold, so its words are the entry's own too (a name adding one is read exactly).
FIRST_SEASON_EXACT = ("but a first season reads an exact title only of a name that adds a word none of its titles "
                      "hold")


class FranchiseReading(NamedTuple):
    """What the franchise rule read in a name: `reason` (the drop, or None), `words` (`franchise_words`), `why` (why it
    was kept, "" when dropped) and `near` (the words share one with a franchise title: a name the log should show)."""
    reason: str | None
    words: frozenset
    why: str
    near: bool


def franchise_reading(title: str, variants: list[str], shows=(), episode: int = 1, alt=(),
                      titles=(), exact: bool = True) -> FranchiseReading:
    """The drop is `"franchise_show …"` when a later-season entry's single names another show of its
    franchise and no more: the name's words before its episode marker (`franchise_words`) are exactly one of the titles
    of a show in `shows` (`(id, title, format, episodes)`, `anilist.franchise_shows`; the title read without a trailing
    `(TV)` or `(YYYY)`), none of the entry's own titles (`variants`), and the number is within that show's episodes (a
    null count is any) and is not one of the entry's `alt` numbers other than `episode`. `[Grp] Tidewater Lantern - 01`
    is the first cour's episode 1 for `Tidewater Lantern 2`; `Tidewater Lantern 2nd Season - 01`, `… (2014) - 01`, `…
    S2 - 01` and an absolute `… - 14` after a 13-episode first cour are not. A bare number that completes one of the
    entry's titles (`Tidewater Lantern 2 [BD]` read as episode 2) is the title's, not a marker.
    A kept name says why, so a log can answer "why wasn't X dropped?".

    A name no franchise title is exactly is still another show's when it states a part that show is
    (`franchise_part`, with `titles`, the entry's other titles, beside `variants`), under the same number test; a kept
    name stating a part says why it named no show.

    `exact=False` (the reading of a first season's name that adds no word, `cli.first_season_parts`) asks only the
    part reading. A name that is exactly a franchise show's title is kept, and its why says so (`FIRST_SEASON_EXACT`):
    its words are the entry's too. A first season's name that adds a word is asked with `exact=True`."""
    words, n, explicit = franchise_words(title, episode, alt)
    near = bool(words) and any(words & _franchise_title(str(rt)) for _, rt, _, _ in shows or ())
    if not shows:
        return FranchiseReading(None, words, "no franchise shows", near)
    if not words or n is None:
        # A name that passed the episode test on a number its marker does not state (`Part 2 - 10` read as the
        # range 2-10 for episode 2) is still the part's show when it states one.
        part = franchise_part(title, (*(variants or ()), *(titles or ())), shows, episode, alt)
        if part.show is not None:
            return FranchiseReading(_part_reason(part, None), words, "", True)
        return FranchiseReading(None, words, "; ".join(x for x in ("no episode number read", part.why) if x), near)
    if n != int(episode) and n in {int(a) for a in alt or ()}:
        return FranchiseReading(None, words, f"{n} is an alt number of the entry", near)
    own = [_franchise_title(str(v)) for v in variants or []]
    if words in own:
        return FranchiseReading(None, words, "the words are one of the entry's own titles", near)
    if not explicit and words | {str(n)} in own:
        return FranchiseReading(None, words, f"the bare {n} completes one of the entry's own titles", near)
    past = None
    for rid, rt, _fmt, eps in shows:
        if _franchise_title(str(rt)) == words:
            if not exact:
                past = past or f"names {rid} ({rt}) as written, {FIRST_SEASON_EXACT}"
                continue
            if eps is None or n <= int(eps):
                return FranchiseReading(
                    f"{FRANCHISE_SHOW} names {rid} ({rt}) as written, with episode {n}"
                    f"{f' of its {eps}' if eps is not None else ''}: no more than that show's title", words, "", True)
            past = past or f"{n} is past {rid}'s {eps} episodes"
    part = franchise_part(title, (*(variants or ()), *(titles or ())), shows, episode, alt)
    if part.show is not None:
        rid, rt, _fmt, eps = part.show
        if eps is None or n <= int(eps):
            return FranchiseReading(_part_reason(part, n), words, "", True)
        past = past or f"{n} is past {rid}'s {eps} episodes ({part.stated})"
    why = past or "no franchise title is exactly these words"
    return FranchiseReading(None, words, f"{why}; {part.why}" if part.why else why, near)


PREQUEL_TAG = "prequel rule"            # ends every `prequel_reading` keep and decline


class PrequelReading(NamedTuple):
    """`kept`, why a one-episode entry's name is its own by its prequel ("" when not), and `own`, why a
    name that is a prequel's title is that prequel's own episode (the decline is logged)."""
    kept: str
    own: str


def prequel_reading(title: str, prequels=(), episode: int = 1) -> PrequelReading:
    """A one-episode entry's name whose words before the marker are exactly one title of a prequel
    (`prequels`: `(id, title, episodes)`, `anilist.prequel_shows`; read as the franchise rule reads a franchise title,
    `franchise_words`). The explicit marker's number is that prequel's count plus `episode` (the group numbered on from
    the prequel): the entry's own, `kept`. Within the prequel's count: the prequel's own episode, `own`. `[Grp] Tidewater
    Lantern - Final Tide Part 3 - 02` is the second of two one-episode specials, whose prequel (the first) has the
    synonym `Tidewater Lantern: Final Tide Part 3`; `… Part 3 - 01` is that prequel's own."""
    own = ""
    for rid, rt, eps in prequels or ():
        number = int(eps) + int(episode)
        words, n, explicit = franchise_words(title, episode, (number,))
        if not (explicit and words and words == _franchise_title(str(rt))):
            continue
        if n == number:
            return PrequelReading(f"single_episode kept: names its prequel {rid} ({rt}) with episode {n}, one past that "
                                  f"show's {eps} ({PREQUEL_TAG})", "")
        if n is not None and n <= int(eps):
            own = own or f"single_episode names prequel {rid} ({rt})'s own episode {n} ({PREQUEL_TAG})"
    return PrequelReading("", own)


def prequel_continuation(title: str, prequels=(), episode: int = 1) -> str:
    """`prequel_reading`'s keep, or ""."""
    return prequel_reading(title, prequels, episode).kept


# The one-episode filter's line reason, on both add-on paths' `single_episode` lines.
SINGLE_EPISODE_REASON = ("a one-episode entry: a name is kept when it holds one of the entry's titles in full, names no "
                         "other special or half, is no related show's episode, and passes the show test, or "
                         f"({PREQUEL_TAG}) it is a one-episode prequel's title with the episode after it; the episode "
                         "test is not asked of it")


FRANCHISE_PART_TAG = "franchise-part rule"  # ends every drop reason of the part reading (`_part_reason`)
PACK_EXEMPT_PART = "a pack: exempt from the franchise reading"     # why a part-stating pack stays


def _part_reason(part: "FranchisePart", n: int | None) -> str:
    """The drop reason of a name whose stated part is another show of the franchise (`franchise_part`)."""
    rid, rt, _fmt, eps = part.show
    number = (f"with episode {n}{f' of its {eps}' if eps is not None else ''}" if n is not None
              else "and states no episode number of the entry")
    return (f"{FRANCHISE_SHOW} names {rid} ({rt}): it states {part.stated}, which that show is (one of the entry's "
            f"titles and that part) and no title of the entry states, {number} ({FRANCHISE_PART_TAG})")


class FranchisePart(NamedTuple):
    """What a name's part statement says. `show` is the franchise show `(id, title, format, episodes)`
    that is one of the entry's titles plus a part the name states and no title of the entry states, or None; `stated`
    is that statement (`Part 2`); `why` says why no show was named ("" when the name states no part)."""
    show: tuple | None
    stated: str
    why: str


def part_numbers(text: str) -> frozenset:
    """The part numbers a name or title states (`Part 2`, `Cour 2`: the pack identity reading's `identity_tokens`)."""
    return frozenset(n for _, n in identity_tokens(text)[2])


_PART_LABEL_END = re.compile(r"(?i)\b(?:part|cour)\s*[#.]?\s*$")     # a span cut right after a part's label


def stated_parts(title: str, episode: int | None = None, alt=()) -> frozenset:
    """The part numbers a name states before its episode marker (`_franchise_span`, the text
    `franchise_words` reads), or anywhere in it when no marker states `episode` (`Part 2 - 10` read as the range 2-10
    for episode 2) or no episode is given. `[Grp] Lantern S01E04 - The Siege Part 2` states no part: `Part 2` there is
    the episode's title. A bare number right after the label (`[Part 01]`, `Part 2 [1080p]`) is the part's own, though
    the episode reading took it: the whole name is read then."""
    span, _n, explicit = (None, None, False) if episode is None else _franchise_span(title, episode, alt)
    if span is not None and not explicit and _PART_LABEL_END.search(span):
        span = None
    return part_numbers(title if span is None else span)


def title_parts(titles) -> frozenset:
    """The part numbers the entry's `titles` state (`Lantern Part 6: Stone Tide`)."""
    return frozenset(n for t in titles or () if t for _k, n in identity_tokens(t)[2])


def extra_parts(title: str, own_parts: frozenset, episode: int | None = None, alt=()) -> frozenset:
    """The parts a name states (`stated_parts`) beyond those the entry's titles state (`own_parts`,
    `title_parts`). The one question `franchise_part` asks first, and whether a first season's search reads its
    franchise at all (`cli.first_season_parts`)."""
    return stated_parts(title, episode, alt) - own_parts


def franchise_part(title: str, titles, shows=(), episode: int | None = None, alt=()) -> FranchisePart:
    """A stated `Part N`/`Cour N` read like a stated season. The name states part N, none of the entry's `titles`
    (`variants` and every AniList title) states it, and a show of the franchise (`anilist.franchise_shows`) is one of
    those titles plus that part: the pack identity reading (`identity_tokens`) gives the show's title the same words and
    seasons as one of the entry's, and part N (`Cour 2` is part 2). `[Grp] Tidewater Lantern Part 2 - 10` is then
    `Tidewater Lantern Part 2`'s, not `Tidewater Lantern`'s, whatever else the name says (`[Grp] 潮灯 Part 2 / Lantern
    Part 2 - 10`). A part some title states (`Lantern Part 6: Stone Tide`) and a part no franchise show is (`Season 4 |
    Part 01` for the first half, whose other halves' titles say more than `Part 1`) name nothing. Given the `episode`
    (and `alt`), only a part stated before the episode marker is read (`stated_parts`)."""
    stated = stated_parts(title, episode, alt)
    if not stated:
        return FranchisePart(None, "", "")
    own = [identity_tokens(t) for t in titles or () if t]
    named = " ".join(f"Part {n}" for n in sorted(stated))
    extra = extra_parts(title, frozenset(n for _w, _s, p in own for _k, n in p), episode, alt)
    if not extra:
        return FranchisePart(None, named, f"it states {named}, as a title of the entry does")
    bases = {(w, s) for w, s, _p in own}
    for show in shows or ():
        words, seasons, parts = identity_tokens(_DISAMBIGUATOR.sub("", str(show[1])))
        hit = extra & {n for _k, n in parts}
        if hit and (words, seasons) in bases:
            return FranchisePart(show, " ".join(f"Part {n}" for n in sorted(hit)), "")
    return FranchisePart(None, named, f"it states {named}, which no franchise show is as one of the entry's titles "
                                      f"plus that part")


def states_own_season(title: str, season: int | None, titles, shows=(), episode: int | None = None, alt=()) -> bool:
    """A name stating the entry's own `season` is not asked the franchise rule, which
    the season test agreed with. Unless it also states a part a franchise show is
    (`franchise_part` with the entry's `titles`): `Show Season 3 Part 2 - 05` is `Show Season 3 Part 2`'s, not the
    `Show Season 3` entry's."""
    return (season is not None and season_number(title) == season
            and franchise_part(title, titles, shows, episode, alt).show is None)


OWN_SEASON_WORDS_TAG = "own-season words rule"     # ends every `own_season_reading` drop reason
_CJK_SCRIPTS = frozenset({"Han", "Kana", "Hangul"})


class OwnSeasonReading(NamedTuple):
    """What the own-season words rule read in a name stating the entry's own season: `reason`, the drop (starting
    `franchise_show`, None when kept), `words`, the words it adds before its season statement as written (CJK ones
    too), and `why`, why it stays (the words test asked and passed)."""
    reason: str | None
    words: tuple
    why: str


@functools.lru_cache(maxsize=1)
def _release_words() -> frozenset:
    """The words a release adds to a show's name that name no show: its audio, subtitle and language words (the dub
    reading's vocabulary, `_DUB_LANGUAGE_WORDS`, `_DUB_AUDIO_TOKENS`, `_DUB_SUB_TOKENS`) and `TV`."""
    return frozenset(w.lower() for w in (*_DUB_LANGUAGE_WORDS, *_DUB_AUDIO_TOKENS, *_DUB_SUB_TOKENS)) | {"tv"}


def _year_word(word: str) -> bool:
    """A four-digit word that reads as a year (`_is_year`): `2014` in `Show 2014 S2 - 01`."""
    return len(word) == 4 and word.isdecimal() and _is_year(int(word))


def season_statement_words(title: str, episode: int | None = None, alt=()) -> list[str]:
    """The words a name writes before its season statement, in order, each once: the text `franchise_words` reads
    (before the marker stating `episode` or an `alt` number; the whole name, its leading tags set aside, when none
    does) cut where `season_number`'s statement starts (`_SEASON_STATED`), less its bracketed tags and the source and
    encoding words (`_SERIES_TAGS`, `_SERIES_NOISE`: `BD`, `Dual-Audio`, `1080p`, `(2020)`), as the show test cuts
    them, and less its years (`_year_word`: the year side, `later_series_years`, reads those).
    `[Grp] Tidewater Lantern Driftwood 2nd Season - 01` gives `tidewater lantern driftwood`."""
    span = _franchise_span(title, episode, alt)[0] if episode is not None else None
    text = _franchise_text(title) if span is None else span
    m = _SEASON_STATED.search(text)
    text = text[:m.start()] if m else text
    words = _title_tokens(_SERIES_NOISE.sub(" ", _SERIES_TAGS.sub(" ", text)))
    return list(dict.fromkeys(w for w in words if not _year_word(w)))


def later_series_years(title: str, titles, start_year: int | None, end_year: int | None = None) -> tuple[list, int]:
    """The year side of the own-season rule: `(years, last)`, the years a name gives its series
    (`extsubs.series_years`) that are later than the entry's last year and that none of its `titles` holds, and that
    last year (`end_year`, AniList's `endDate`, else `start_year + 1`). `([], 0)` without a start year. A year after
    the season has ended is another show's (`… | Show (2020) 2nd Season` for a 2015-16 season); every earlier year is
    kept: the series' first year (`Show (2016) - S05`), the end year (`Show (2025) (Season 3)` for a 2024-25
    season), and a year a title holds (`Show (2022) 2nd Season`, whose romaji is `Show (2022)`)."""
    from .extsubs import clean_name, series_years
    if not start_year:
        return [], 0
    last = end_year if end_year else start_year + 1
    held = {w for t in titles or () for w in _title_tokens(str(t)) if _year_word(w)}
    return sorted(y for y in series_years(clean_name(title)) if y > last and str(y) not in held), last


def own_season_reading(title: str, titles, episode: int | None = None, alt=(), start_year: int | None = None,
                       tv_series: bool = False, end_year: int | None = None) -> OwnSeasonReading:
    """Is a name the own-season reading keeps (`states_own_season`) still another show's? It is when it writes, before
    its season statement, a word that none of the entry's `titles` (`variants` and every AniList title) holds:
    `[Grp] Tidewater Lantern Driftwood 2nd Season - 01` is the `Driftwood` arc's second season, not
    `Tidewater Lantern 2nd Season`'s. The words are `season_statement_words` (no year among them), each folded
    (`_fold_marks`) and compared with the titles' `_franchise_title` and `_title_tokens` words. Not added: a release's
    language, audio and subtitle words and `TV` (`_release_words`).
    A set of added words made only of Han, Kana or Hangul words is a translated title put in front
    (`藥師少女的獨語 第三季 / … 3rd Season - 53`), not another show: kept. Any Latin, digit or other word makes it count.

    For a TV series (`tv_series`) with a known `start_year`, a name giving its series a year after the entry's last
    year that no title holds (`later_series_years`, with `end_year`) is another show too: a movie or a one-episode
    entry is never asked. Read only of names the caller found stating the entry's own season, singles and packs
    alike."""
    own: set = set()
    for t in titles or ():
        own |= {_fold_marks(w) for w in (*_franchise_title(str(t)), *_title_tokens(str(t)))}
    skip = _release_words()
    words = tuple(w for w in season_statement_words(title, episode, alt) if _fold_marks(w) not in own and w not in skip)
    if words and not all(_scripts(w) and _scripts(w) <= _CJK_SCRIPTS and not any(c.isdigit() for c in w)
                         for w in words):
        return OwnSeasonReading(f"{FRANCHISE_SHOW} adds \"{' '.join(words)}\" before its season statement, which no "
                                f"title of the entry holds: another show of the franchise ({OWN_SEASON_WORDS_TAG})",
                                words, "")
    later, last = later_series_years(title, titles, start_year, end_year) if tv_series else ([], 0)
    if later:
        return OwnSeasonReading(f"{FRANCHISE_SHOW} states the year {', '.join(map(str, later))} for its series, after "
                                f"the entry's last year {last}: another show of the franchise "
                                f"({OWN_SEASON_WORDS_TAG})", words, "")
    why = (f"adds only CJK words before its season statement, a translated title ({' '.join(words)})" if words else
           "adds no word before its season statement")
    return OwnSeasonReading(None, words, why + (f"; no year after the entry's last year {last}" if last else ""))


FIRST_SEASON_SHOW_TAG = "first-season show rule"   # ends every first-season exact-title drop reason


def first_season_added_words(title: str, titles, episode: int | None = None, alt=()) -> tuple:
    """The words a first season's name adds to the entry's `titles` (`variants` and every AniList title), by the
    own-season words rule's reader (`own_season_reading`, no year asked): `season_statement_words` none of the titles
    hold, release words left out, and () when the words it adds are only Han, Kana or Hangul ones (a translated
    title). `[Grp] Tidewater Lantern 0 - 02` adds `0` for `Tidewater Lantern`. Whether `cli.first_season_parts`
    reads the franchise, as a stated part does, and which names it asks the exact-title reading."""
    reading = own_season_reading(title, titles, episode, alt)
    return reading.words if reading.reason else ()


def marked_show(title: str, titles, shows=()) -> tuple[str | None, frozenset]:
    """`(reason, words)`: a first season's name whose words before its first explicit episode marker (`_FRANCHISE_MARK`,
    whatever episode it states; the text `_franchise_span` reads) are exactly another franchise show's title, none of
    the entry's `titles`, with a number within that show's episodes. `[Grp] Tidewater Lantern 0 - 01` is
    `Tidewater Lantern 0`'s episode 1 when episode 2 is asked: the episode marker's reading (`franchise_reading`) finds
    no marker for 2, and a one-file torrent plays its only file whatever it is. The reason is None when kept."""
    t = _franchise_text(title)
    m = _FRANCHISE_MARK.search(t)
    if m is None:
        return None, frozenset()
    n = int(next(m.group(g) for g in _FRANCHISE_EPISODE if m.group(g) is not None))
    cut = next((m.end(g) for g in _FRANCHISE_SEASON if m.group(g) is not None), m.start())
    words = frozenset(_title_tokens(t[:cut]))
    if not words or words in {_franchise_title(str(x)) for x in titles or () if x}:
        return None, words
    for rid, rt, _fmt, eps in shows or ():
        if _franchise_title(str(rt)) == words and (eps is None or n <= int(eps)):
            return (f"{FRANCHISE_SHOW} names {rid} ({rt}) as written, with its episode {n}"
                    f"{f' of {eps}' if eps is not None else ''}, before the name's first episode marker: no more than "
                    f"that show's title ({FIRST_SEASON_SHOW_TAG})"), words
    return None, words


class FirstSeasonPack(NamedTuple):
    """What `first_season_show_pack` read: `reason` (the drop, None when kept), `words` (the whole name's, its ranges
    set aside) and `near` (they share a word with a franchise title: a kept pack the log should show)."""
    reason: str | None
    words: frozenset
    near: bool


def first_season_show_pack(title: str, titles, shows=()) -> FirstSeasonPack:
    """What a first season's pack is: dropped (`franchise_show`) when its whole-name words
    (`season_statement_words` with no episode), its own ranges set aside (`pack_range_matches`, the ranges the
    prefilter reads: `Show 0 - 01 ~ 23`'s `01 ~ 23`, not `pack_range_search`'s `0 - 01`), are exactly the
    title of a show in `shows` (`anilist.franchise_shows`, `_franchise_title`), and are none of the entry's `titles`.
    A show titled as one of the entry's titles is never named (`SLAM DUNK (Movie)` reads as `Slam Dunk`).
    `[Grp] Tidewater Lantern 0 (01-23) [Batch]` is `Tidewater Lantern 0`'s pack; `[Grp] Tidewater Lantern - 01 ~ 64` is
    the entry's own. The reason is None when the pack is kept."""
    text = str(title or "")
    for m in reversed(pack_range_matches(text)):
        text = f"{text[:m.start()]} {text[m.end():]}"
    words = frozenset(season_statement_words(text))
    own = {_franchise_title(str(t)) for t in titles or () if t}
    near = bool(words) and any(words & _franchise_title(str(rt)) for _, rt, _, _ in shows or ())
    if not words or words in own:
        return FirstSeasonPack(None, words, near)
    for rid, rt, _fmt, _eps in shows or ():
        if _franchise_title(str(rt)) == words:
            return FirstSeasonPack(f"{FRANCHISE_SHOW} names {rid} ({rt}): the pack's words, its ranges set aside, are "
                                   f"that show's title and none of the entry's ({FIRST_SEASON_SHOW_TAG})", words, True)
    return FirstSeasonPack(None, words, near)


FIRST_SEASON_TAG = "first-season rule"  # ends every `first_season_reason`, so a log can pick its drops out of a sink
# In every first-season reading of a pack, a drop or a several-season keep.
FIRST_SEASON_PACK_TAG = "first-season pack rule"


class FirstSeasonReading(NamedTuple):
    """What the first-season rule read in a single for an entry ani.zip files as TVDB season 1 whose
    titles state none: `reason`, the drop ("" when kept), and `why`, why a name stating season 2 or later was kept
    ("" when it states none or is dropped). The keeps are logged too."""
    reason: str
    why: str


NUMERAL_SEASON_TAG = "numeral-season rule"  # in every drop reason whose season was read from a Roman numeral


def numeral_season(title: str, titles) -> tuple[int | None, str]:
    """`(N, words)` when a name states season N by a Roman numeral (`II` to `VI`, the identity reading's `_ID_ROMAN`)
    right after words that open one of the entry's `titles` (`Lantern Runner III: Dusk Tide - 04`, `… / Lantern Runner
    III … [04]` for `Lantern Runner: Dusk Tide`), and no title holds that numeral; else `(None, "")`. `season_number`
    reads digits and season words only, and the identity reading's `_title_forms` reads a title's own trailing numeral,
    not a name's: so `Show II - 04` passed for season 1, and only the guard's `iii` and a stray `04v2` stood between the
    first season and another season's play (measured 2026-10-05).

    Only the words before the episode marker are read, as `sibling_reading` reads a name's show words, so an episode
    title or a trailing tag (`- 07 - Kaze II no Uta`, `- 07 [Kaze VI]`) is no season. The name is NFKC-normalised first,
    so `Ⅱ` and `ＩＩ` read as `II`."""
    return _opening_words_season(title, titles, _ID_ROMAN)


ORDINAL_SEASON_TAG = "ordinal-season rule"  # in every drop reason whose season was read from a bare ordinal
# A bare ordinal, `2nd` to `6th` or `second` to `sixth` (the words of the identity reading's `_ID_ORDINALS`, from
# the one table `_SEASON_WORDS`), and the words after it that make it no season: a later part or cour of the season
# (`Lantern Runner 2nd Cour - 16`). The range is written once, here.
_BARE_ORDINAL_RANGE = range(2, 7)
_BARE_ORDINALS = {**{f"{n}{ {2: 'nd', 3: 'rd'}.get(n, 'th')}": n for n in _BARE_ORDINAL_RANGE},
                  **{_SEASON_WORDS[n - 1]: n for n in _BARE_ORDINAL_RANGE}}
_ORDINAL_NOT_SEASON = frozenset({"part", "cour", "half"})


def bare_ordinal_season(title: str, titles) -> tuple[int | None, str]:
    """`(N, words)` when a name states season N by a bare ordinal (`2nd` to `6th`, `second` to `sixth`)
    right after words that open one of the entry's `titles` (`Lantern Runner 2nd - 04` for `Lantern Runner: Dusk
    Tide`), with no `part`, `cour` or `half` after it, and no title holds that ordinal in any spelling (`Lantern Runner
    2nd Stage` keeps `… Second Stage - 04`); else `(None, "")`. `season_number` reads an ordinal only before `season`
    (`2nd Season`), so `Show 2nd - 04` passed for season 1 and was ranked (2026-10-16). Read as `numeral_season`
    reads a numeral: before the episode marker, NFKC-normalised. A `2nd.Season` or `2nd_Season` that `season_number`
    misses reads here as the season it states. Named apart from the trailing-number
    rule's `_ordinal_seasons`, which reads an ordinal before `Season`."""
    return _opening_words_season(title, titles, _BARE_ORDINALS, _ORDINAL_NOT_SEASON)


def _opening_words_season(title: str, titles, table: dict, not_before=frozenset()) -> tuple[int | None, str]:
    """`numeral_season` and `bare_ordinal_season`: `(table[w], words)` for the first word `w` of the name's words
    before its episode marker that is in `table`, comes right after k words opening one of `titles`, is not followed
    by a word of `not_before`, and whose number no title holds as a word of `table`.

    k >= min(2, the title's word count). One opening word read a name with no marker whole,
    its episode title too: `The Gale Lamp 07 The 2nd Promise` and `The Gale Lamp [07][The 2nd Promise]` were season 2
    for `The Gale Lamp` (`the 2nd`). A one-word title is still read (`Lantern 2nd - 03`)."""
    body = _SERIES_GROUP.sub(" ", unicodedata.normalize("NFKC", str(title or "")).replace("_", " "))
    marker = _SERIES_EP.search(body)
    tokens = _title_tokens(_FRANCHISE_LEAD.sub(" ", _name_body(body[:marker.start()] if marker else body)))
    owns = [o for o in (_title_tokens(unicodedata.normalize("NFKC", str(t))) for t in titles or ()) if o]
    own_numbers = {table[w] for o in owns for w in o if w in table}
    for i, w in enumerate(tokens):
        if not i or w not in table or table[w] in own_numbers or (i + 1 < len(tokens) and tokens[i + 1] in not_before):
            continue
        for own in owns:
            for k in range(min(i, len(own)), min(2, len(own)) - 1, -1):
                if tokens[i - k:i] == own[:k]:
                    return table[w], " ".join(tokens[i - k:i + 1])
    return None, ""


class SeasonStated(NamedTuple):
    """The season a name states for the first-season rule (`first_season_stated`): `season`, the `words` it
    was read from when a fallback read it ("" for `season_number`), and `reading`, that fallback named for a drop or
    keep reason (not `how`, which `first_season_reading` builds from it)."""
    season: int | None
    words: str
    reading: str


def first_season_stated(title: str, titles) -> SeasonStated:
    """The season a name states for the first-season rule: `season_number` (the words empty), else
    `numeral_season`, else `bare_ordinal_season`. The one reading `first_season_reading` and the kept-name
    lines ask."""
    stated = season_number(title)
    if stated is not None:
        return SeasonStated(stated, "", "")
    n, words = numeral_season(title, titles)
    if n is not None:
        return SeasonStated(n, words, f"a Roman numeral after a title's opening words, {NUMERAL_SEASON_TAG}")
    n, words = bare_ordinal_season(title, titles)
    # A bare `2nd` names a later season or a later cour (`… Honki Dasu 2nd - 04` is Part 2's).
    return SeasonStated(n, words, f"an ordinal after a title's opening words (a later season or cour), "
                                  f"{ORDINAL_SEASON_TAG}" if words else "")


def first_season_reading(title: str, titles, pack: bool = False) -> FirstSeasonReading:
    """A single stating season 2 or later (`season_number`) is another season's episode, unless one of the entry's
    `titles` ends in that season's number (`Lantern Runner 3`, AniList's synonym for an entry TVDB files as its own
    season 1: the group's `Lantern Runner S03E11` is it), read as the identity reading reads a title's trailing number
    (`_title_forms`). `titles` must be every title the entry has, AniList's synonyms uncapped (`cli.entry_titles`). The
    name's own words are not compared. A guard that dropped only names whose words were exactly one title kept 21 more
    wrong-season names in the measurement and protected no right one this keep does not.
    A name stating no season so is read for a Roman numeral after a title's opening words (`first_season_stated`,
    `numeral_season`), and its reason says so (`NUMERAL_SEASON_TAG`); then for a bare ordinal there
    (`bare_ordinal_season`, `ORDINAL_SEASON_TAG`). A keep's `why` names that reading too, as a drop's reason does.

    `pack` (whether the caller reads the name as a pack) reads a pack the same way, so `Show S2 (01-11)
    [Batch]` is season 2's for a first season, and its reason says so (`FIRST_SEASON_PACK_TAG`). A pack stating several
    seasons (`several_seasons`: `S1-S2`, `Season 01-02`, `S2 Season 01-02`, `Season 1+2`) is kept."""
    stated, words, reading = first_season_stated(title, titles)
    if stated is None or stated < 2:
        return FirstSeasonReading("", "")
    read_as = f"stated as {words!r}, {reading}; " if words else ""
    spans = several_seasons(title) if pack else []
    if spans:
        return FirstSeasonReading("", f"{read_as}a pack of seasons {', '.join(spans)}, not of one "
                                      f"({FIRST_SEASON_PACK_TAG})")
    for t in titles or ():
        if any(form.number == stated for form in _title_forms(str(t))):
            return FirstSeasonReading("", f"{read_as}title {str(t)!r} ends in {stated}")
    how = read_as
    how += f"a pack of that season alone, {FIRST_SEASON_PACK_TAG}; " if pack else ""
    return FirstSeasonReading(f"season {stated} wanted 1 ({how}ani.zip files the entry as TVDB season 1 and its titles "
                              f"state no season; {FIRST_SEASON_TAG})", "")


def first_season_reason(title: str, titles, pack: bool = False) -> str:
    """`first_season_reading`'s drop reason, or ""."""
    return first_season_reading(title, titles, pack).reason


FIRST_SEASON_SHOW_SKIP = f"the show test fails: its season is not read ({FIRST_SEASON_PACK_TAG})"


def first_season_pack_reading(title: str, variants, episode: int, alt, season: int | None, titles,
                              first_titles) -> tuple[bool, FirstSeasonReading]:
    """The pack pass's first-season reading of a pack, `(shown, reading)`. A pack pass asks no other test
    of a pack, so the season is read, as `first_season_reading(pack=True)` against `first_titles`, only when the show
    test passes (`release_matches_target` with `titles`, its episode not asked), as the first pass reads it only of a
    name that passes. `shown` is that test. Another show's pack stating season 2 or later is left unread, its `why`
    saying so (`FIRST_SEASON_SHOW_SKIP`), so it is never handed on as this
    entry's other season, which the ambiguity guard would read (`guard_reads_drop`). The one reading both branches'
    pack passes ask."""
    shown = release_matches_target(title, variants, episode, alt, target_season=season, check_episode=False,
                                   titles=titles)[0]
    if shown:
        return True, first_season_reading(title, first_titles, pack=True)
    stated = first_season_stated(title, first_titles)[0]
    return False, FirstSeasonReading("", FIRST_SEASON_SHOW_SKIP if stated is not None and stated >= 2 else "")


def pack_show_reason(title: str, variants, episode: int, alt, season: int | None, titles) -> str:
    """Why a pack-pass record is another show, or "". The show test the built-in Torznab add-on asks a
    pack on the pack pass (`release_matches_target` with `titles`, the episode not asked: a pack's episode test is its
    range), so its reason is Torznab's (`series 0.41`, or the spelling-only reason). Both branches' pack passes
    ask it of a record no free-text add-on's `matches` has asked."""
    shown, why = release_matches_target(title, variants, episode, alt, target_season=season, check_episode=False,
                                        titles=titles)
    return "" if shown else why


# The dropped names the ambiguity guard still reads (`cli._resolve`), so these drops remove a pick and never the guard's
# evidence: a franchise show (the franchise rule and its part reading, a later season's or a first season's), a film cut
# of the show (the film rule), and a first season's other season (the first-season rule and its numeral reading, whose
# reason ends in `FIRST_SEASON_TAG`). Not the season test for an entry whose titles state a season, which the guard
# never read: its own episode plays, as before. `guard_reads_drop` is the rule; `GUARD_DROP_RULE` names it on the line.
GUARD_DROP_RULE = (f"{FRANCHISE_SHOW} and {COMPILATION_FILM} drops, and season drops of a first season "
                   f"({FIRST_SEASON_TAG}); not the season test of an entry whose titles state a season")


def guard_reads_drop(code: str, why: str) -> bool:
    """Does the ambiguity guard read a name dropped with this `code` and reason (`GUARD_DROP_RULE`)?"""
    if code in (FRANCHISE_SHOW, COMPILATION_FILM):
        return True
    return code == "season" and FIRST_SEASON_TAG in str(why or "")


def franchise_show_reason(title: str, variants: list[str], shows=(), episode: int = 1, alt=(),
                          titles=()) -> str | None:
    """`franchise_reading`'s drop reason, or None."""
    return franchise_reading(title, variants, shows, episode, alt, titles).reason


# A TV series' episode is no OVA. The format words every language's groups write as they are (`OVA`,
# `OAV`, `OAD`, and their plurals); not `Special`/`SP`, which are English words (`_SPECIAL_WORD` reads them for
# the one-episode and related-show rules, and leaves `OAV` out).
_OVA_WORD = re.compile(r"^(?:ova|oav|oad)s?$")
OVA_ENTRY_TAG = "OVA-entry rule"        # ends every `ova_entry_reason`, so a log can pick its drops out of a sink
# A bracketed tag's `+`/`&` joins groups, resolutions or subtitle languages (`[GrpA&GrpB]`,
# `[720p&1080p]`, `[Sub ITA + ENG]`), not an OVA to the episodes; the film rule still reads a bracketed bundle.
# Square and lenticular brackets only: a `(..)` may hold the bundle itself (`Show (TV + OVA) - 01`), and a wrong drop of
# an own release costs more than keeping a rare `(720p&1080p)` OVA single.
_BRACKET_TAG = re.compile(r"\[[^\]]*\]|【[^】]*】")


def _bundle_outside_tags(title: str) -> bool:
    """Does the name join parts with `+`/`&` (`_FILM_BUNDLE`) outside its `[..]`/`【..】`
    tags? A tag that holds an OVA word is the bundle itself (`[TV + OVA]`, `【TV+OVA】`) and is read, not blanked."""
    def blank(m: re.Match) -> str:
        return m.group(0) if any(_OVA_WORD.match(w) for w in _title_tokens(m.group(0))) else " "
    return bool(_FILM_BUNDLE.search(_BRACKET_TAG.sub(blank, str(title or ""))))


def ova_entry_reason(title: str, episode: int, alt=(), titles=()) -> str | None:
    """For a TV series' episode (the caller decides: `release_matches_target(tv_series=True)`), why a single is another
    entry of the franchise, an OVA, or None. Its words before the episode marker (`franchise_words`) hold an OVA word
    (`_OVA_WORD`) that no title of the entry (`titles`) holds: `[Grp] Tidewater Lantern Driftwood - OAV 01` is the OVA's
    episode 1, not the series'. Kept: a pack (`parse_title`) or a bundle (`+`/`&` outside its bracketed tags,
    `_bundle_outside_tags`: `Show 01-12 + OVAs`, `22 ep + 2 oav + 1 film`), whose OVA sits beside the episodes, and an
    OVA word after the marker (`Show - 13 (OVA)`, the series' own episode a group marks). The code is the franchise
    rule's: the guard reads the drop (`guard_reads_drop`) and the picker does not list it, as it does not list a
    franchise show's."""
    if parse_title(title)["batch"] or _bundle_outside_tags(title):
        return None
    words, _n, _explicit = franchise_words(title, episode, alt)
    own: set[str] = set()
    for t in titles or ():
        own.update(_title_tokens(str(t)))
    said = sorted(w for w in words if _OVA_WORD.match(w) and w not in own)
    if not said:
        return None
    return (f"{FRANCHISE_SHOW} states {said[0]!r} before its episode marker, and the entry is a TV series: an OVA is "
            f"another entry of the franchise, not its episode {episode} ({OVA_ENTRY_TAG})")


def release_matches_target(title: str, variants: list[str], episode: int,
                           alt: list[int] | None = None, target_season: int | None = None,
                           check_episode: bool = True, titles=(), tv_series: bool = False) -> tuple[bool, str]:
    """Does this release title name the requested series, season and episode? `(keep, reason)`.

    `check_episode=False` skips the episode test alone, for a title that cannot carry the number: a
    movie's release, or a pack whose range the prefilter checks. The show and season tests still run.

    A name the show test passes by spelling alone, sharing no word with any title, is another show
    (`spelling_only_reason`; `titles` are the entry's other titles, `cli.entry_titles`), a pack included.

    `tv_series` (the entry is a TV series, `cli.tv_series_entry`) drops a single that states an OVA
    before its episode marker (`ova_entry_reason`, read against `variants` and `titles`).

    The season test is deliberately **not** applied to a pack: a pack's season naming is the least
    reliable part of its title, and `pick_pack_files` already drops a pack that does not contain
    the episode once a provider lists its files. A false keep there costs one file-list lookup; a
    false drop costs the only live source for an old show.
    """
    if check_episode and not title_matches_episode(title, episode, alt):
        return False, "episode"
    sim, _, reread = _best_show_part(title, variants)
    if sim < SERIES_SIMILARITY:
        return False, f"series {sim:.2f}"
    spelling = spelling_only_reason(title, variants, titles)   # before the pack exemption: a pack is asked too
    if spelling:
        return False, spelling
    ova = ova_entry_reason(title, episode, alt or (), (*(variants or ()), *(titles or ()))) if tv_series else None
    if ova:
        return False, ova
    if target_season is None:
        # The season test is dormant: no caller passes a season yet, so a release naming
        # another season of the right show is still kept, exactly as before. Passing a season turns
        # it on, and `series_similarity` alone already removes the wrong-show accepts.
        return True, "ok"
    if parse_title(title).get("batch"):
        return True, "pack"
    # A name the second reading passed is read as it read it, `_` as a space: `\b` never fires inside `_Season_1_`,
    # so the raw name stated no season and another season passed as `season not stated`.
    rs = season_number(title.replace("_", " ") if reread else title)
    if rs is None:
        # Silence is not season 1. `[Kumo-raws] Harbor Nights Part 2 - 01` is AniList's *second*
        # entry and names no season at all, so reading an absent season as 1 rejects a release that
        # is correct. Only an explicit disagreement is evidence; this branch can therefore never
        # reject something the filter keeps today.
        return True, "season not stated"
    if rs != target_season:
        return False, f"season {rs} wanted {target_season}"
    return True, "ok"


# Files inside a pack that carry an episode-like number but are not episodes (Vellum "Movie 2", "NCOP02"). The same
# words keep an extras or recap release from reading as a season pack (`Show S2 NCOP`, `Show S2 Recap`);
# a bare `NC` (not a group's `NC-` prefix), `- OP`/`- ED`, `Menus`, `Recap` and `Bonus` were added for it.
_EXTRA_FILE = re.compile(r"\b(NC(?:OP|ED)\d*|OP\d+|ED\d+|OVA|OAD|Movies?|Film|Specials?|SP\d*|Extras?|Preview|PV|Trailer"
                         r"|Creditless|Menus?|Recaps?|Bonus)\b|\bNC(?![\w-])|-\s*(?:OP|ED)\b", re.I)
VIDEO_EXT = (".mkv", ".mp4", ".webm", ".avi", ".ts")


# Which entry a name inside a pack belongs to. Every spelling of a season is one season; a `Part N` or
# `Cour N` is kept apart from it, since groups use them for halves and cours an entry's own title may not state.
_ID_ORDINALS = {w: n + 1 for n, w in enumerate(_SEASON_WORDS[:6])}      # first..sixth
# The ordinals (`Second Season`, `2nd Season`) are named apart, since only they meet a title's trailing number
# without ani.zip's say; the order of `_ID_SEASONS` still matters to `identity_tokens`' substitutions.
_ID_ORDINAL_SEASONS = (
    re.compile(r"\b(" + "|".join(_ID_ORDINALS) + r")\s+season\b"),
    re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+season\b"),
)
_ID_SEASONS = _ID_ORDINAL_SEASONS + (
    re.compile(r"\bseason\s*0*(\d{1,2})\b"),
    re.compile(r"\bs0*(\d{1,2})(?:e\d+)?\b"),
)
_ID_PARTS = re.compile(r"\b(part|cour)\s*0*(\d{1,2})\b")
# A title's trailing season number: `Zoku Shou 2`, or Roman `Quillhold II`. Only 2 to 9: a larger one is
# the show's name (`Zone 13`), not a season.
_ID_TRAILING_NUMBER = re.compile(r"\s([2-9]|ii|iii|iv|v|vi)\s*$", re.I)
_ID_ROMAN = {"ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6}
# In a name (not a title): a folder's running index (`2. Ledgerlight/`), a range (`Seasons 1-2`, `01~12`) and the
# episode marker (` - 2`, ` - 01v2`) are not words of the show's name; left in, they would supply a title's trailing
# number (`Ledgerlight 2`).
_ID_FOLDER_INDEX = re.compile(r"^\s*\d{1,3}\.\s+")
# `Part 2` is part 2; `Season 1-2` is a range and is not joined, so the range goes whole.
_ID_JOIN = re.compile(r"\b(part|cour|season)\s+(?=\d{1,2}(?!\d)(?!\s*[~-]\s*\d))", re.I)
_ID_RANGE = re.compile(r"(?<![\w.])\d{1,4}\s*[~-]\s*\d{1,4}(?![\w.])")
_ID_EPISODE_MARK = re.compile(_DASH_MARK_HEAD + r"\d{1,4}(?:v\d)?" + _DASH_MARK_END)
# The reason codes a skipped pack file carries (`pick_episode_file`'s `skipped`, the `score.file_pick` lines).
FILE_ENTRY_NOT_NAMED = "entry_not_named"              # neither its path nor the pack title names an entry title
FILE_PACK_NAMES_OTHER_SHOW = "pack_names_other_show"  # the pack title names the entry, and another show too
FILE_SEASON_NOT_IN_PACK = "file_season_not_packs"     # the pack title names the entry; the file states another season
FILE_PACK_SEVERAL_SEASONS = "pack_several_seasons"    # the pack title names the entry but spans several seasons
# A name met the entry only by a season code or `Season N` read as a title's trailing number, which ani.zip's
# episode 1 does not allow (`season_code_refusal`).
FILE_TITLE_NUMBER_AS_CODE = "title_number_as_code"
# A number of the name is the episode, but the name's episode marker says another (`Show 2 - 08` for episode 2,
# `[HEVC 10 bits]` for 10); `against` is the part the episode was read from. Not an identity refusal.
FILE_NUMBER_NOT_EPISODE = "number_not_episode"
# A file of a season the pack numbers from 1 whose code read as the episode, but which ani.zip's pair for the
# episode says is another (`S02E05` for 11061's episode 5, which is TVDB's `S1E5`), or no pair is known. `against` is
# the part read. Not an identity refusal either.
FILE_SEASON_RELATIVE = "season_relative"
# A first season's file in a pack naming more than the entry (`first_season_file_check`): another show of
# its franchise by its name (`against`: the reason), a season the entry is not (`against`: the seasons), a file under an
# extras folder (`against`: the folder).
FILE_FRANCHISE_SHOW = "franchise_show"
FILE_OTHER_SEASON = "other_season"
FILE_EXTRAS_FOLDER = "extras_folder"
# How an entry title was met in a name (`entry_named`, the picked record's `identity["met_by"]`).
MET_AS_WRITTEN = "as_written"            # its words and numbers, and any season it states, in any spelling
MET_BY_TITLE_NUMBER = "title_number"     # its trailing number, met by an ordinal (`2nd Season`)
MET_BY_SEASON_CODE = "season_code"       # its trailing number, met by a season code or `Season N` ani.zip allows
# Why a season code may not meet a title's trailing number N.
CODE_EPISODE1_UNKNOWN = "episode1_unknown"            # no ani.zip season/episode for the entry's episode 1
CODE_LATER_COUR = "later_cour_in_tvdb_season"         # ani.zip files episode 1 as S{N}E{k>1}: a later cour of season N
# How a pack title spans several seasons. A range (`Season 1-2`, `Seasons 1-3`, `S01-S03`), a
# list (`Season 1, 2, 3`, `S1+S2`, `Season 1 + Season 2`), or `+`/`&`/`,` parts that state different seasons
# (`Show + Show 2nd Season`). Two labels of one season with no joining form (`2nd Season S03`) are one season.
_ID_EPISODE_RANGE = re.compile(r"\s-\s*\d{1,4}(?:v\d)?\s*[~-]\s*\d{1,4}(?:v\d)?")
_ID_SEASON_RANGE = re.compile(r"\b(?:seasons?\s*0*(\d{1,2})\s*[~-]\s*(?:seasons?\s*|s)?0*(\d{1,2})"
                              r"|s0*(\d{1,2})\s*[~-]\s*s0*(\d{1,2}))\b")
_ID_SEASON_LIST = re.compile(r"\b(?:seasons?|s)\s*0*\d{1,2}(?:\s*(?:[,&+]|\band\b)\s*(?:seasons?\s*|s)?0*\d{1,2}\b)+")
_ID_TAGS = re.compile(r"^\s*\[[^\]]*\]|\[[^\]]*\]")
_ID_PART_SPLIT = re.compile(r"[+&,]")
_ID_EXTRA_WORDS = frozenset({"movie", "movies", "film", "ova", "ovas", "oad", "special", "specials", "sp", "extra",
                             "extras", "bonus", "ncop", "nced", "recap", "recaps", "and", "the"})


class _TitleForm(NamedTuple):
    """One way a title may be named (`_title_forms`): its words, seasons and parts, and `number`, the trailing number
    read as its season (None for the title as written)."""
    words: frozenset
    seasons: frozenset
    parts: frozenset
    number: int | None


def _season_value(g: str) -> int:
    """A season regex's group as a number: an ordinal word (`second`) or digits."""
    return _ID_ORDINALS[g] if g in _ID_ORDINALS else int(g)


def identity_tokens(text: str) -> tuple[frozenset, frozenset, frozenset]:
    """`(words, seasons, parts)` of a name or title. `2nd Season`, `Second Season`, `Season 2`, `S2` and
    `S02E05` all give season 2; `Part 2`/`Cour 2` give `("part", 2)`/`("cour", 2)`; the rest is lower-case words and
    numbers (a year stays a number)."""
    t = str(text or "").replace("_", " ").replace("×", "x").lower()
    seasons: set[int] = set()
    parts: set[tuple[str, int]] = set()

    def season(m):
        seasons.add(_season_value(m.group(1)))
        return " "

    for rx in _ID_SEASONS:
        t = rx.sub(season, t)

    def part(m):
        parts.add((m.group(1), int(m.group(2))))
        return " "

    t = _ID_PARTS.sub(part, t)
    return frozenset(re.sub(r"[^\w\s]+", " ", t).split()), frozenset(seasons), frozenset(parts)


def _title_forms(title: str) -> list[_TitleForm]:
    """A title as written, and, when it ends in a season number (2–9, or `II`–`VI`), with that number read as its
    season (`Zoku Shou 2`, `Quillhold II`). Only an ordinal may meet that `number` (`title_numbers`)."""
    words, seasons, parts = identity_tokens(title)
    forms = [_TitleForm(words, seasons, parts, None)] if words else []
    m = _ID_TRAILING_NUMBER.search(str(title or ""))
    token = m.group(1).lower() if m else None
    if token and token in words and words - {token}:
        number = _ID_ROMAN.get(token) or int(token)
        forms.append(_TitleForm(words - {token}, seasons | {number}, parts, number))
    return forms


def season_code_refusal(episode1: list | None, number: int) -> str:
    """Empty when a season code (`S02`, `Season 2`) may meet a title's trailing `number`, else why
    not. `episode1` is ani.zip's `[seasonNumber, episodeNumber]` for the entry's episode 1 (`cli.entry_identity`), or
    None. A code may not meet it when that is unknown, or when ani.zip files episode 1 as `S{number}E{k>1}`: the entry
    is a later cour inside TVDB's season `number` (20751's `S2E11`), and a group's `S02` is that season's first cour."""
    if not episode1:
        return CODE_EPISODE1_UNKNOWN
    season, episode = episode1
    return CODE_LATER_COUR if season == number and episode > 1 else ""


def title_numbers(titles: list[str], episode1: list | None = None) -> list[list]:
    """`[title, number, verdict]` for each title whose trailing number may read as its season: an
    ordinal (`2nd Season`) always meets it; `verdict` says whether a season code does too (`codes_allowed`), or why not
    (`season_code_refusal`)."""
    return [[t, form.number, season_code_refusal(episode1, form.number) or "codes_allowed"]
            for t in titles or [] for form in _title_forms(t) if form.number is not None]


def _ordinal_seasons(name: str) -> set[int]:
    """The seasons a name states as an ordinal (`2nd Season`, `Second Season`), not as a code (`S02`)."""
    t = str(name or "").replace("_", " ").lower()
    return {_season_value(g) for rx in _ID_ORDINAL_SEASONS for g in rx.findall(t)}


def entry_named(text: str, titles: list[str],
                episode1: list | None = None) -> tuple[str | None, str | None, str | None]:
    """`(title, how, near)`: the entry title whose every word, number and season one segment of `text` holds
    (a folder or the file name, split at `/`), when that segment states no `Part`/`Cour` the title does not, and `how`
    it was met (`MET_AS_WRITTEN`, or `MET_BY_TITLE_NUMBER`). A season the name states beyond the title's is not
    compared. One segment, not the path pooled: a franchise root folder's words plus another
    show's `Second Season` folder named the entry. When no title is named, `near` is a title the name would have named
    by reading a season code as its trailing number, else None.

    A title's trailing number is met only by an ordinal (`Zoku Shou 2nd Season`) or by the number itself
    (the title as written, `Zoku Shou 2 - 01`). A group's `S02`/`S02E01`/`Season 2` counts the franchise's seasons,
    where 20751's `S2` is its first cour; read as `Zoku Shou 2`'s `2`, it played the other cour.

    The name is cleaned in this order, and the order matters: (1) a folder's running index (`2. Show/`) goes first;
    (2) the episode marker (` - 05`), so `Show 2 - 05` keeps its `2` and `Part 2 - 05` is not a range;
    with a range after it (` - 01 ~ 12`, as `several_seasons` takes it), so `Season 2 - 01 ~ 12` keeps its season
    (only ` - 01` went, and the range read `2 ~ 12` took the season's number); (3) `Part`/
    `Cour`/`Season` are joined to a number that does not start a range (`Season 1-2` stays a range); (4) ranges
    (`01~12`, `Season 1-2`'s `1-2`) go; (5) `identity_tokens` then reads seasons, parts and words."""
    forms = [(title, form) for title in titles or [] for form in _title_forms(title)]
    near = None
    for segment in str(text or "").split("/"):
        name = _ID_EPISODE_MARK.sub(" ", _ID_EPISODE_RANGE.sub(" ", _ID_FOLDER_INDEX.sub("", segment)))
        name = _ID_RANGE.sub(" ", _ID_JOIN.sub(r"\1", name))
        words, seasons, parts = identity_tokens(name)
        ordinals = None
        for title, form in forms:
            if not (form.words <= words and form.seasons <= seasons and form.parts == parts):
                continue
            if form.number is None:
                return title, MET_AS_WRITTEN, None
            ordinals = _ordinal_seasons(name) if ordinals is None else ordinals
            if form.number in ordinals:
                return title, MET_BY_TITLE_NUMBER, None
            if not season_code_refusal(episode1, form.number):
                return title, MET_BY_SEASON_CODE, None
            near = near or title
    return None, None, near


def names_entry(text: str, titles: list[str]) -> str | None:
    """The entry title one segment of `text` names, or None: the tests' shorthand for `entry_named(text,
    titles)[0]`, with ani.zip's episode 1 unknown (no season code meets a title's trailing number). No production
    caller; the rule's sites go through `pack_file_check`."""
    return entry_named(text, titles)[0]


def several_seasons(pack_title: str) -> list[str]:
    """The seasons a pack title spans when it spans more than one, else `[]`. A part of a
    `+`/`&`/`,` title that states no season counts as `unstated`; a part that is only extras (`+ Movie`) is not one."""
    t = str(pack_title or "").lower()
    t = _ID_EPISODE_MARK.sub(" ", _ID_EPISODE_RANGE.sub(" ", t))
    spans: set[int] = set()
    for m in _ID_SEASON_RANGE.finditer(t):
        lo, hi = (int(g) for g in (m.group(1, 2) if m.group(1) else m.group(3, 4)))
        if lo < hi:
            spans.update(range(lo, hi + 1))
    for m in _ID_SEASON_LIST.finditer(t):
        nums = {int(n) for n in re.findall(r"\d{1,2}", m.group(0))}
        if len(nums) > 1:
            spans.update(nums)
    if len(spans) > 1:
        return [str(n) for n in sorted(spans)]
    statements = set()
    for part in _ID_PART_SPLIT.split(_ID_TAGS.sub(" ", t)):
        words, seasons, _ = identity_tokens(part)
        if seasons or {w for w in words if not w.isdigit()} - _ID_EXTRA_WORDS:
            statements.add(seasons)
    if len(statements) > 1:
        return sorted({str(n) for st in statements for n in st} | ({"unstated"} if frozenset() in statements else set()))
    return []


def pack_file_check(entry: dict | None, pack_title: str = ""):
    """For a later-season entry, `check(path) -> (code, rule, title, how)`: code None when the file may be the
    entry's, with the rule that accepted it (`path` or `pack_title`), the title that says so and how it was met
    (`entry_named`); else a reason code. None when the rule is off: no identity, or a first season."""
    if not entry or not entry.get("sequel"):
        return None
    titles = list(entry.get("titles") or [])
    episode1 = entry.get("episode1")
    by_pack, pack_how, pack_near = entry_named(pack_title, titles, episode1) if pack_title else (None, None, None)
    pack_other = bool(by_pack) and sibling_name(pack_title, titles) is not None
    pack_seasons = identity_tokens(pack_title)[1] if by_pack else frozenset()
    spans = several_seasons(pack_title) if by_pack else []

    def check(path: str) -> tuple[str | None, str | None, str | None, str | None]:
        own, how, near = entry_named(path, titles, episode1)
        if own:
            return None, "path", own, how
        if not by_pack:
            if near:                    # the file's path came near the entry title
                return FILE_TITLE_NUMBER_AS_CODE, None, near, None
            if pack_near:               # only the pack title did
                return FILE_TITLE_NUMBER_AS_CODE, None, pack_title, None
            return FILE_ENTRY_NOT_NAMED, None, None, None
        if pack_other:
            return FILE_PACK_NAMES_OTHER_SHOW, None, by_pack, None
        if spans:
            return FILE_PACK_SEVERAL_SEASONS, None, by_pack, None
        if not identity_tokens(path.rsplit("/", 1)[-1])[1] <= pack_seasons:
            # `[G] Gravemoor II` names the entry as written, and its `S02E05` files would name
            # it by their code; the file's season is not the pack title's only because the code was not allowed.
            return (FILE_TITLE_NUMBER_AS_CODE, None, near, None) if near else (FILE_SEASON_NOT_IN_PACK, None, by_pack,
                                                                                None)
        return None, "pack_title", by_pack, pack_how
    return check


def multi_show_pack(pack_title: str, titles, own=()) -> bool:
    """Does a pack name more than the entry? An own-holding pack (`own_holding_pack`: other shows after a bundle mark),
    or one whose name gives the guard words besides the entry's own (`guard_words`). Decided without the franchise, so
    it says when a first season's pick needs it (`first_season_file_check`)."""
    own = tuple(own or titles or ())
    return (own_holding_pack(pack_title, list(titles or ()), False, own) is not None
            or guard_words(pack_title, list(titles or ()), False, own)[0] is not None)


def pack_root(names) -> str | None:
    """The torrent's root folder in a pack's file list: the first path segment, when every listed file sits under it
    (after RD's leading `/`), else None. TorBox's names carry it (`[Grp] Show Complete + Specials/Show - 02.mkv`, the
    `torbox.filepick handover` lines and its cached lists), an add-on's list may not (bare names), RD's paths
    are documented only as starting with `/`: a list with the root or without one reads the same here. A BitTorrent
    torrent of several files puts every file under its one root, so a first segment not shared by every file is no root
    (a list without one: `Specials/…` beside `Show/…`) and nothing is dropped. A single listed file under a folder has
    that folder dropped too, root or not: one file has no folder to tell apart from."""
    split = [[p for p in str(n or "").split("/") if p] for n in names]
    if not split or any(len(p) < 2 for p in split):
        return None
    firsts = {p[0] for p in split}
    return firsts.pop() if len(firsts) == 1 else None


def first_season_file_check(entry: dict | None, pack_title: str = "", root: str | None = None):
    """For a first season's pack that names more than the entry (`multi_show_pack`), `check(path) -> (code, against)`:
    code None when the file may be the entry's episode, else why it is not. Off (None) for a later season (its own rule,
    `pack_file_check`), a movie, a single-show pack, and an entry that carries no franchise shows
    (`entry["first_season_shows"]`, `cli.first_season_franchise`: none read, or the read failed): there the file is
    picked by its number, as before. A file is set aside when
    - it sits under a folder `_EXTRA_FILE` reads (`Specials/`, `Extras/`, `NCOP/`): `extras_folder`;
    - it states a season the entry is not (`S02E02`, `Season 2/`, read as `identity_tokens` reads one), when ani.zip
      files the entry's episode 1 (`entry["episode1"]`) and numbers it in one TVDB season (no `season_map`):
      `other_season`;
    - its name, or its folder's, is another franchise show's (`franchise_reading`, `marked_show`,
      `first_season_show_pack`, with the entry's own titles, `entry["own_titles"]`): `franchise_show`.
    The folders read are those below the torrent's root (`root`, `pack_root`): the root is the pack's own name, and an
    own-holding pack's name says what it bundles (`… + Specials & Extras`, `S1+S2`, `… + Show 0`), so read as a folder
    it set every own file aside."""
    entry = entry or {}
    shows = tuple(tuple(s) for s in entry.get("first_season_shows") or ())
    if entry.get("sequel") or entry.get("movie") or not shows:
        return None
    titles = list(entry.get("titles") or [])
    own = tuple(entry.get("own_titles") or titles)
    if not multi_show_pack(pack_title, titles, own):
        return None
    ep1 = entry.get("episode1")
    own_season = ep1[0] if ep1 and not entry.get("season_map") else None

    def check(path: str, episode: int, alt=()) -> tuple[str | None, str | None]:
        parts = [p for p in str(path or "").split("/") if p]
        if root is not None and len(parts) > 1 and parts[0] == root:
            parts = parts[1:]
        folders, base = parts[:-1], (parts or [""])[-1]
        stem = base.rsplit(".", 1)[0] if base.lower().endswith(VIDEO_EXT) else base
        folder = next((d for d in folders if _EXTRA_FILE.search(d)), None)
        if folder is not None:
            return FILE_EXTRAS_FOLDER, folder
        if own_season is not None:
            seasons = set().union(*(identity_tokens(p)[1] for p in [*folders, stem]))
            if seasons - {own_season}:
                return FILE_OTHER_SEASON, f"season {', '.join(map(str, sorted(seasons)))} wanted {own_season}"
        why = (franchise_reading(stem, titles, shows, episode, alt, own).reason
               or marked_show(stem, own, shows)[0]
               or next((r for r in (first_season_show_pack(d, own, shows).reason for d in folders[-1:]) if r), None))
        if why:
            return FILE_FRANCHISE_SHOW, why
        return None, None
    return check


def set_aside_words(skipped: list[dict], sample: int = 3) -> str:
    """The files a first season's pick set aside (`first_season_file_check`'s codes), in the viewer's words, for a
    refusal's message: `Show 0 - 02.mkv (another show of the franchise), Specials/Show - 02.mkv (an extras folder)`;
    empty when none was."""
    said = {FILE_FRANCHISE_SHOW: "another show of the franchise", FILE_OTHER_SEASON: "another season",
            FILE_EXTRAS_FOLDER: "an extras folder"}
    rows = [f"{s['path']} ({said[s['code']]})" for s in skipped if s.get("code") in said]
    more = len(rows) - sample
    return ", ".join(rows[:sample]) + (f" and {more} more" if more > 0 else "")


def season_relative_seasons(season_map: dict | None, readings: Iterable[EpisodeReading]) -> dict | None:
    """Which seasons a pack counts from 1, for an entry ani.zip numbers on across TVDB seasons
    (`season_map`, `anizip.season_map`; None for any other entry, and then None). `from_1`: each season code after the
    entry's first TVDB season whose lowest episode among the pack's files is 1 (`S02E01`). Its files count that
    season's episodes, so 11061's `S02E05` is its episode 63. A group that numbers a later season on (`S02E27`-`S02E38`
    for episodes 27-38, as Netflix's seasons) never starts it at 1, and its files read as before. Measured over the
    corpus' file lists: BlackRabbit's `S02`/`S03` start at `E01`, ZigZag's `S02`-`S06` at 27, 39, 59 and 137. `lowest`
    is each code season's lowest episode, the decision's ground.

    The rule assumes the pack's seasons are TVDB's. `contradiction` says they are
    not, when a season counted from 1 exists: (i) a code `S{k}E{n}` past ani.zip's count for season k (k at least the
    entry's first: a split-cour batch's `S02E13`-`S02E24` where TVDB's season 2 has 12), or (ii) the pack starts the
    entry's first season at `E01` and ends it off ani.zip's count (a group's own `S01E01`-`S01E26` where TVDB's has
    30). It is `{season, highest, anizip_count}`, and the seasons counted from 1 are then skipped, never read as
    before."""
    if not season_map:
        return None
    lowest: dict[int, int] = {}
    highest: dict[int, int] = {}
    for reading in readings:
        for season, numbers in reading.codes:
            if season is not None and numbers:
                lowest[season] = min(lowest.get(season, min(numbers)), min(numbers))
                highest[season] = max(highest.get(season, max(numbers)), max(numbers))
    first, counts = season_map["first"], season_map["counts"]
    from_1 = sorted(season for season, number in lowest.items() if season > first and number == 1)
    contradiction = None
    if from_1:
        past = [season for season in sorted(highest)
                if season >= first and season in counts and highest[season] > counts[season]]
        if past:
            contradiction = {"season": past[0], "highest": highest[past[0]], "anizip_count": counts[past[0]]}
        elif lowest.get(first) == 1 and first in counts and highest[first] != counts[first]:
            contradiction = {"season": first, "highest": highest[first], "anizip_count": counts[first]}
    return {"from_1": from_1, "lowest": dict(sorted(lowest.items())), "contradiction": contradiction}


def season_relative_reading(reading: EpisodeReading, count: dict | None, pair: list | None, shared: bool,
                            episode: int) -> tuple[frozenset, dict | None, bool]:
    """A file whose code is in a season the pack counts from 1 (`count["from_1"]`,
    `season_relative_seasons`) is the episode ani.zip pairs with that code: `S02E01` is episode `episode` only when
    ani.zip's pair for it (`pair`, `anizip.episode_pair`) is `[2, 1]`. Every number a code states in its season is
    mapped (`S02E01-02` is both `[2, 1]` and `[2, 2]`), from `EpisodeReading.codes`, the one season-code grammar
    (`_EP_CODE`). Its other numbers are not read, since the map says which episode the code is (the `15` of `S02E09 -
    15 x 15` is the episode's title). No pair for the episode, a pair two episodes share, or a pack whose seasons
    contradict ani.zip's (`count["contradiction"]`) reads it as no episode at all (a skip, never a guess).
    `(numbers, map, named)`: the numbers the file is read as; how it was mapped (`season` and `numbers` of its code,
    ani.zip's `pair` for the episode, `shared`, `episode`: the episode it was read as, or None, and `contradiction`
    when there is one); and whether ani.zip's pair names its code (the file a contradiction skips). The reading's own
    numbers, None and False when no code of the name is in such a season."""
    relative = set((count or {}).get("from_1") or ())
    codes = [(season, numbers) for season, numbers in reading.codes if season in relative]
    if not codes:
        return reading.numbers, None, False
    contradiction = count.get("contradiction")
    named = next(((season, numbers) for season, numbers in codes
                  if pair and not shared and pair[0] == season and pair[1] in numbers), None)
    hit = None if contradiction else named
    season, numbers = named or codes[0]
    mapped = {"season": season, "numbers": sorted(numbers), "pair": pair, "shared": shared,
              "episode": int(episode) if hit else None}
    if contradiction:
        mapped["contradiction"] = contradiction
    return frozenset({int(episode)} if hit else ()), mapped, named is not None


def file_bytes(f: dict) -> int | None:
    """A file record's size, whichever site listed it: TorBox's and an add-on's `size`, Real-Debrid's `bytes`. Not an
    infinite one or one of more than `NUMBER_DIGITS_MAX` digits (`int(inf)` ended a movie's play)."""
    for key in ("size", "bytes"):
        v = file_size(f.get(key))
        if v is not None:
            return int(v)
    return None


def pick_movie_file(files: list[dict], entry: dict) -> dict | None:
    """A movie's file inside a torrent of several videos, by being the movie, never by its episode 1
    (`[neoHEVC] … [Season 1 + Specials + Movie]` holds `Season 1/… - S01E01.mkv`, the TV series' episode 1, next to
    `Specials/… - S00E05 - (S1M1 - Movie).mkv`). First a file whose season code is ani.zip's season-0 pair for the movie
    (`entry["movie_pairs"]`, `anizip.movie_pairs`); else the largest video, when every video carries a size; else None
    (the site's own fallback). No extras words are read: `_EXTRA_FILE` holds `Movie`, the movie's own file's word. The
    picked record carries `matched` (`movie_pair` or `movie_largest`) and `read`: the rule, the pairs, the size and how
    many videos there were."""
    pairs = {tuple(p) for p in entry.get("movie_pairs") or ()}
    videos = []
    for idx, f in enumerate(files or []):
        name = str(f.get("name") or f.get("filename") or f.get("path") or f.get("short_name") or "")
        base = name.rsplit("/", 1)[-1]
        if base.lower().endswith(VIDEO_EXT):
            videos.append((idx, f, base))
    if not videos:
        return None

    def picked(idx, f, base, rule, extra):
        return {**f, "basename": base, "index": idx, "matched": rule, "identity": {"rule": "movie"},
                "read": {"by": "movie", "rule": rule, "pairs": sorted(list(p) for p in pairs), "size": file_bytes(f),
                         "videos": len(videos), **extra}}
    for idx, f, base in videos:
        reading = episode_reading(base.rsplit(".", 1)[0], list(entry.get("titles") or []))
        hit = next(((season, n) for season, numbers in reading.codes for n in sorted(numbers) if (season, n) in pairs),
                   None)
        if hit:
            return picked(idx, f, base, "movie_pair", {"part": reading.part})
    sizes = [file_bytes(f) for _, f, _ in videos]
    if all(sizes):
        idx, f, base = max(videos, key=lambda v: file_bytes(v[1]))
        return picked(idx, f, base, "movie_largest", {"next_size": sorted(sizes)[-2] if len(sizes) > 1 else None})
    return None


def pick_episode_file(files: list[dict], episode: int, alt: list[int] | None = None, entry: dict | None = None,
                      pack_title: str = "", skipped: list | None = None) -> dict | None:
    """The video file inside a pack that is episode `episode`. `alt` carries the absolute number from
    ani.zip for shows whose packs count from 1 across seasons. A season-relative match wins over an absolute
    one when both exist. Returns the file record plus basename/index/matched, or None.

    `entry` is the entry's identity (`{"titles", "sequel"}`, `cli.entry_identity`) and `pack_title` the
    pack's. For a later-season entry a file numbered like the episode is taken only when its path, or the pack title,
    names the entry (`pack_file_check`); each one passed over goes to `skipped` as `{"path", "file", "code",
    "against"}`: `against` is the title or pack title the code is about (the pack title for `entry_not_named`). For
    `title_number_as_code` it is what came near: the entry title when the file's path came near it (by a
    season code met as the title's number), the pack title when only the pack title did. Without an identity, or for a
    first season, the rule is today's. The picked record carries `identity`: how it was accepted
    (`rule`, `title`, and `met_by`: how that title was met).

    A first season's pack that names more than the entry is read whole (`first_season_file_check`): a file of
    another franchise show, another season or an extras folder goes to `skipped` with its code, and among the files left
    holding the episode one whose name gives the guard no word of its own (`guard_reading`) wins over the list's order
    (`Show Zero - 02` loses to `Show - 02`). Its `identity` is `{"rule": "first_season", "franchise"` (where the shows
    came from, `entry["first_season_from"]`), `"set_aside"`, `"left"` (the files left holding the episode),
    `"preferred"` (the pick was not the first of them) and `"root"` (the torrent's root folder the folder tests read
    below, `pack_root`)`}`.

    A file's episode is its name's episode marker (`episode_reading`, after the entry titles that hold a
    number are cut out), not any number of the name: `Lantern Zoku 2 - 08` is episode 8 for `Lantern Zoku 2`. The
    picked record carries `read` (`EpisodeReading.as_log`: the tier, the part read, the numbers, the titles cut); a file
    whose stray number was the episode but whose marker says another goes to `skipped` as `number_not_episode`, with
    `against` the part read and `read` the whole reading (what was cut, which tier).

    For an entry ani.zip numbers on across TVDB seasons (`entry["season_map"]`, `anizip.season_map`),
    a file of a season the pack counts from 1 is read through ani.zip's pair for the episode
    (`season_relative_reading`): 11061's `S02E05` is episode 63, not 5. With a season map, the picked record's `read`
    carries the pack's decision (`pack_seasons`: the seasons counted from 1, each code season's lowest episode, a
    contradiction; `season_relative_seasons`) and, for a file read through it, `map`; a file whose code read as the
    episode but which ani.zip pairs with another (or with none, or a contradiction skips) goes to `skipped` as
    `season_relative`, with `against` the part read and `read` (which carries both).

    For a movie (`entry["movie"]`) the file is the movie's, not episode `episode` (`pick_movie_file`)."""
    if (entry or {}).get("movie"):
        return pick_movie_file(files, entry)
    alts = [int(a) for a in (alt or []) if a is not None and int(a) != int(episode)]
    check = pack_file_check(entry, pack_title)
    root = pack_root(f.get("name") or f.get("filename") or f.get("path") or f.get("short_name") or ""
                     for f in files or [])
    first = None if check is not None else first_season_file_check(entry, pack_title, root)
    titles = list((entry or {}).get("titles") or [])
    season_map = (entry or {}).get("season_map")
    pair, shared = episode_pair(season_map, episode) if season_map else (None, False)
    fallback = None

    def read_files():
        for idx, f in enumerate(files or []):
            name = str(f.get("name") or f.get("filename") or f.get("path") or f.get("short_name") or "")
            base = name.rsplit("/", 1)[-1]
            if not base.lower().endswith(VIDEO_EXT):
                continue
            stem = base.rsplit(".", 1)[0]
            if _EXTRA_FILE.search(stem):
                continue
            yield idx, f, name, base, stem, episode_reading(stem, titles)

    # Only a season map needs the whole pack read first (which seasons count from 1);
    # any other entry's files are read one by one, and reading stops at the episode's, as before.
    listed = list(read_files()) if season_map else read_files()
    hits: list[dict] = []          # a first season's files left holding the episode (`first`), then its absolutes
    absolutes: list[dict] = []

    def left(name: str, base: str) -> bool:
        code, against = first(name.rsplit(".", 1)[0], int(episode), alts)
        if code and skipped is not None:
            skipped.append({"path": name, "file": base, "code": code, "against": against})
        return code is None
    count = season_relative_seasons(season_map, [reading for *_, reading in listed]) if season_map else None

    def accepted(name: str, base: str, path: str) -> dict | None:
        if check is None:
            return {"rule": "off"}
        code, rule, title, how = check(name)
        if code:
            if skipped is not None:
                skipped.append({"path": path, "file": base, "code": code, "against": title or pack_title})
            return None
        return {"rule": rule, "title": title, "met_by": how}

    for idx, f, name, base, stem, reading in listed:
        numbers, mapped, named = season_relative_reading(reading, count, pair, shared, episode)
        read = {**reading.as_log(), **({"pack_seasons": count} if count else {}), **({"map": mapped} if mapped else {})}
        if mapped and int(episode) not in numbers and (named or int(episode) in reading.numbers
                                                        or reading.numbers & set(alts)):
            if skipped is not None:
                skipped.append({"path": name, "file": base, "code": FILE_SEASON_RELATIVE, "against": reading.part,
                                "read": read})
            continue
        if int(episode) in numbers:
            if first is not None:
                if left(name, base):
                    hits.append({**f, "basename": base, "index": idx, "matched": "episode", "read": read})
                continue
            ident = accepted(name.rsplit(".", 1)[0], base, name)
            if ident is not None:
                return {**f, "basename": base, "index": idx, "matched": "episode", "identity": ident, "read": read}
            continue
        if alts and numbers & set(alts):
            if first is not None:
                if left(name, base):
                    absolutes.append({**f, "basename": base, "index": idx, "matched": "absolute", "read": read})
                continue
            if fallback is None:
                ident = accepted(name.rsplit(".", 1)[0], base, name)
                if ident is not None:
                    fallback = {**f, "basename": base, "index": idx, "matched": "absolute", "identity": ident,
                                "read": read}
            continue
        if skipped is not None and title_matches_episode(stem, episode, alts, any_number=True):
            skipped.append({"path": name, "file": base, "code": FILE_NUMBER_NOT_EPISODE, "against": reading.part,
                            "read": reading.as_log()})
    if first is not None:
        pool = hits or absolutes
        if not pool:
            return None
        own = tuple((entry or {}).get("own_titles") or titles)
        best = next((h for h in pool if guard_reading(h["basename"].rsplit(".", 1)[0], titles, False, own)[0] is None),
                    pool[0])
        aside = sum(1 for x in skipped or () if x["code"] in (FILE_FRANCHISE_SHOW, FILE_OTHER_SEASON,
                                                              FILE_EXTRAS_FOLDER))
        return {**best, "identity": {"rule": "first_season", "franchise": (entry or {}).get("first_season_from"),
                                     "set_aside": aside, "left": len(pool), "preferred": best is not pool[0],
                                     "root": root}}
    return fallback


def pick_refused_suggest(skipped: list[dict]) -> str:
    """The suggest of a play-time pick that found no file for the episode in a pack (`rd.filepick`,
    `torbox.filepick`). When the files passed over are season-relative (and stray
    numbers), it says so, as `score.file_pick`'s miss and `pin.covers` do."""
    codes = {s["code"] for s in skipped}
    if FILE_SEASON_RELATIVE in codes and codes <= {FILE_SEASON_RELATIVE, FILE_NUMBER_NOT_EPISODE}:
        return ("pick another release (its files count a later season from 1, and ani.zip pairs this episode with "
                "none of them)")
    return "pick another release"


def skipped_summary(skipped: list[dict], entry: dict | None = None, sample: int = 3) -> dict:
    """The files a pack's file pick passed over, as counts per code and a short sample of
    `{"path", "file", "code", "against"}`, with the entry titles they were checked against (`checked`, the first 4).
    The identity rule's codes, `number_not_episode` (a stray number was the episode; its sample also carries
    `read`) and `season_relative` (its `read` carries the `map`). One shape for every site that picks a pack's file."""
    codes: dict[str, int] = {}
    for s in skipped:
        codes[s["code"]] = codes.get(s["code"], 0) + 1
    return {"skipped": len(skipped), "skipped_codes": codes, "skipped_sample": skipped[:sample],
            "checked": list((entry or {}).get("titles") or [])[:4]}


GROUP_STATS_PATH = CACHE_DIR / "group-stats.json"


def load_group_stats() -> dict:
    return cached_json(GROUP_STATS_PATH, None) or {}


def record_group_langs(group_key: str, langs: list[str] | None, lang: str) -> None:
    """Learn whether a release group ships the preferred subtitle language. Called after any verification."""
    if not group_key or langs is None:
        return
    stats = load_group_stats()
    entry = stats.get(group_key) or {}
    entry["total"] = int(entry.get("total", 0)) + 1
    if has_language(langs, lang):
        entry[lang] = int(entry.get(lang, 0)) + 1
    stats[group_key] = entry
    write_cache(GROUP_STATS_PATH, stats)


def group_stats_report(stats: dict | None = None) -> dict:
    """How much of the learned prior actually exists. `group_prior` ignores a key seen once."""
    stats = load_group_stats() if stats is None else stats
    active = {k: v for k, v in stats.items() if int((v or {}).get("total", 0)) >= 2}
    return {"keys": len(stats), "active": len(active),
            "observations": sum(int((v or {}).get("total", 0)) for v in stats.values()),
            "path": str(GROUP_STATS_PATH)}


def backfill_group_stats(lang: str, log_dir: Path | None = None, apply: bool = False) -> dict:
    """Rebuild `group-stats.json` from subtitle track lists already recorded in the JSONL log.

    `record_group_langs` only ever fired after an ffprobe of a streamed release or an mpv track
    readout, so the prior learned about roughly one release per play and ignored every subtitle-track
    record the same resolve had just read. Measured 2026-09-19 on the development machine: 15 keys, 8
    of them past the two-observation threshold, against 73 keys (58 active) supported by the same corpus.

    The log is the source rather than an add-on's own cache, because the plugin cannot read an add-on's
    cache and a candidate line carries the title the group comes from. Each distinct release counts
    once however many times it was scored: a release is its indexer's name and that indexer's release
    id. A line written before candidates named their indexer matches the same id under any indexer, so a
    release scored on both sides of that change counts once.
    This used to parse one indexer's request URLs to drop rows its hosts' colliding torrent ids
    could have poisoned; those rows were written by the in-plugin code before its per-host cache, which
    this plugin's own log never held, and the plugin no longer knows any indexer's URLs.
    """
    log_dir = Path(log_dir or (CACHE_DIR / "log"))
    seen: set = set()             # (key, indexer, id) of every counted line, tagged or not
    seen_any: set = set()         # (key, id) of every counted line: what an untagged line matches
    seen_untagged: set = set()    # (key, id) of counted untagged lines: what any tagged line matches
    out: dict[str, dict] = {}
    skipped = {"duplicate": 0, "no_group": 0}
    rows = 0
    for fn in sorted(log_dir.glob("*.jsonl")):
        with open(fn, errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("component") != "score" or rec.get("stage") != "candidate":
                    continue
                data = rec.get("data") or {}
                langs, title, tid = data.get("verified_langs"), data.get("title"), data.get("id")
                if langs is None or not isinstance(title, str):
                    continue
                rows += 1
                key = parse_title(title).get("stats_key") or ""
                if not key or key.startswith("|"):
                    skipped["no_group"] += 1
                    continue
                release = (key, data.get("indexer"), str(tid))
                loose = (key, str(tid))
                if release in seen or loose in seen_untagged or (release[1] is None and loose in seen_any):
                    skipped["duplicate"] += 1
                    continue
                seen.add(release)
                seen_any.add(loose)
                if release[1] is None:
                    seen_untagged.add(loose)
                entry = out.setdefault(key, {"total": 0})
                entry["total"] += 1
                if has_language(langs, lang):
                    entry[lang] = int(entry.get(lang, 0)) + 1
    before = group_stats_report()
    result = {"lang": lang, "rows_considered": rows, "skipped": skipped,
              "before": before, "after": group_stats_report(out), "applied": bool(apply)}
    if apply:
        merged = load_group_stats()
        merged.update(out)          # a rebuilt key replaces its stale counterpart wholesale
        write_cache(GROUP_STATS_PATH, merged)
        result["after"] = group_stats_report(merged)
    return result


def static_group_priors(settings: dict) -> dict[str, int]:
    """The static prior per group key, from the `preferredGroups` setting. Empty by
    default, so the plugin itself favours no group. Entries are parsed here as well as in
    `coerce_settings`, so a settings dict that never went through it ranks the same."""
    table: dict[str, int] = {}
    low, high = GROUP_POINTS_BOUNDS
    for entry in as_list(settings.get("preferredGroups"), []):
        parsed = parse_group_entry(entry)
        if parsed:
            table.setdefault(parsed[0], min(max(parsed[1], low), high))   # the first entry counts
    return table


def english_only_groups(settings: dict, lang: str) -> set[str]:
    """The `englishOnlyGroups` setting's group keys, when the viewer's language is not English;
    empty otherwise, or when the group is also preferred (the viewer said so explicitly).

    A tie-break, not a score term: among releases of equal score *and* seeders, one from these
    groups sorts after the others. Points here, or a tie-break ahead of seeders, would reorder
    releases that score and seeders already rank (measured: a -8 penalty reordered 4 of 7 live
    resolves)."""
    if has_language([lang], "eng"):
        return set()
    preferred = set(static_group_priors(settings))
    out = set()
    for entry in as_list(settings.get("englishOnlyGroups"), []):
        parsed = parse_group_entry(entry, weighted=False)
        if parsed and parsed[0] not in preferred:
            out.add(parsed[0])
    return out


def group_prior(group_key: str, lang: str, stats: dict | None, static_key: str | None = None,
                static: dict | None = None, floor_learned: bool = False) -> int:
    """Learned prior (keyed group|source) beats the static table once observed at least twice.
    `static` is `static_group_priors(settings)`; without it there is no static prior. `floor_learned` (the language-
    neutral fallback) counts a learned prior below 0 as 0; the static one stays."""
    entry = (stats or {}).get(group_key) or {}
    total = int(entry.get("total", 0))
    hits = int(entry.get(lang, 0))
    if total >= 2:
        rate = hits / total
        if rate >= 0.67:
            learned = 15
        elif rate == 0:
            learned = -20
        else:
            learned = round(15 * rate)
    elif total == 1:
        learned = 8 if hits else -8
    else:
        return (static or {}).get(static_key or group_key, 0)
    return max(learned, 0) if floor_learned else learned


# A season pack names its season and no episode (`[Vellum] Show Second Season (BD 1080p)`, `Show S2 (BD)`).
# The statement is read only for an entry whose titles state that same season: measured on the 2026-09 corpus, reading
# it for every entry admits other cours' packs whose file the file pick takes (2026-10-02).
# The season statement is read by `_SEASON_STATED` (`season_number`), which decides and names it. `_PACK_SEASON` only
# masks it before the episode test, and is not `_SEASON_STATED`: that one also reads `S02E05`, which must stay a marker.
_PACK_SEASON = re.compile(r"\b(?:(?:" + "|".join(_SEASON_WORDS) + r")\s+season\b|\d{1,2}(?:st|nd|rd|th)\s+season\b"
                          r"|seasons?\s*0*\d{1,2}(?![\w.])|s0*\d{1,2}(?![\w.]))", re.I)
# Not something the file pick can play: scripts, subtitles alone, a disc image, music.
_PACK_NOT_VIDEO = re.compile(r"\bscripts?\b|\bsub(?:title)?s?\s+only\b|\bBDMV\b|\bISO\b|\bOST\b|\bsoundtrack\b|\bfonts?\b",
                             re.I)
_PACK_PART = re.compile(_PART_STATED + r"(?![\w.])", re.I)          # the part statement (`_PART_STATED`)
# Numbers in a pack's name that are not an episode, set aside before looking for one. Only a number standing alone can
# read as an episode below, so a number glued to letters (`1080p`, `4k`, `x265`, `v2`) needs no mask (measured on the
# 2026-09 corpus and feeds: removing those masks moved no title).
_PACK_NOT_EPISODE = (
    re.compile(r"\b\d{3,4}\s*[x×]\s*\d{3,4}\b"),
    re.compile(r"(?<![\w.])(?:19|20)\d\d(?![\w.])"),
    re.compile(r"\b(?:x|h\.?)26[45]\b|\bhi10p?\b|\b\d{1,2}[\s-]?bits?\b|\bav1\b|\bvp9\b", re.I),
    re.compile(r"\b(?:aac|flac|opus|ac3|e-?ac-?3|dd\+?p?|ddp|dts(?:-hd)?|truehd|lpcm|pcm|mp3|vorbis)\s*\d(?:\.\d)?\b", re.I),
    re.compile(r"(?<![\w.])\d\.\d(?![\w.])"),
    _PACK_SEASON,
    re.compile(r"\bvol(?:ume)?\.?\s*0*\d{1,2}(?![\w.])", re.I),
    _PACK_PART,
)
# The numbers a pack file's name without an episode marker does not state as its episode
# (`episode_reading`'s bare tier): these, but `Part N`/`Cour N`, which some groups number their files by in arc order
# (`Arc Name - Part 1`, measured: masking it refused that pack's right file for episode 1).
_BARE_NOT_EPISODE = tuple(rx for rx in _PACK_NOT_EPISODE if rx is not _PACK_PART)
# What marks one episode: `S02E05`, `S2.05` (a dotted name's episode right after its season), `E05`, `EP05`, `#05`,
# `第5話`, a decimal episode (`05.5`; audio channels such as `2.0` are masked above), or any number left after the ones
# above. Not `_EP_TOKENS`: that one reads `S02E05`'s `E05` only and would let a season statement's own number through.
_PACK_EPISODE = re.compile(r"\bs\d{1,2}\s*e\d{1,4}|\bs\d{1,2}\.\d{1,4}(?:v\d)?\b|\be(?:p|pisode)?\.?\s*\d{1,4}\b|#\d{1,4}|第\s*\d+\s*[話话集]"
                           r"|(?<![\w.])\d{1,4}\.\d(?![\w.])|(?<![\w.])\d{1,4}(?:v\d)?(?![\w.])", re.I)
# Several shows joined by `~` (`Show A~Show B~Show C Second Season`): a franchise pack, never one entry's season.
_PACK_SHOWS_JOINED = re.compile(r"[^\W\d_]\s*~\s*[^\W\d_]")


def _season_pack_reading(title: str, season: int | None, titles: Iterable[str] = ()) -> tuple[str, str]:
    """`(statement, refusal)`. `statement` is what makes `title` a pack of `season` (`Second Season`, `S2`),
    read by `season_number`'s own pattern so the logged statement is the one that decided; `refusal` is why a title
    stating that season is not one. Both "" when the title states no season or another, or `season` is None.
    `titles` are the entry's: a `Part N`/`Cour N` the title states must be one an entry title states, and when an entry
    title states part 2 or later, a title stating no part is not its pack."""
    t = str(title or "")
    m = _SEASON_STATED.search(t) if season is not None else None
    if not m or season_number(t) != season:
        return "", ""
    parts = identity_tokens(t)[2]
    entry_parts = [identity_tokens(x)[2] for x in titles or ()]
    if parts and not any(parts <= own for own in entry_parts):
        return "", "part not the entry's"
    if not parts and any(n >= 2 for own in entry_parts for _, n in own):
        return "", "no part for a later part"
    name = re.sub(r"\.torrent\s*$", "", t.strip(), flags=re.I).lower()
    if name.endswith(VIDEO_EXT) or _HASH_TAG.search(t):
        return "", "one file"
    if _PACK_NOT_VIDEO.search(t):
        return "", "not video"
    if _EXTRA_FILE.search(t) or _SPECIAL_WORD.search(t) or _WORD_MOVIE.search(t):
        return "", "extras"
    if _PACK_SHOWS_JOINED.search(t):
        return "", "several shows"
    rest = t.replace("_", " ")
    for rx in _PACK_NOT_EPISODE:
        rest = rx.sub(" ", rest)
    if _PACK_EPISODE.search(rest):
        return "", "episode marker"
    if several_seasons(t):
        return "", "several seasons"
    return m.group(0), ""


def season_pack(title: str, season: int | None, titles: Iterable[str] = ()) -> str:
    """The statement (`Second Season`, `S2`) that makes `title` a pack of `season`, or "". It must state that
    one season (`season_number`) and no other (`several_seasons`), name one show (no `~` chain), and carry no episode
    marker (a decimal one included), no extras word (`_EXTRA_FILE`, `_SPECIAL_WORD`, `_WORD_MOVIE`: OVA, Special, SP01,
    Movie, NCOP, PV, Recap, Bonus), no mark of a single file and no mark of something that is not video, and agree
    with the entry's `titles` on `Part`/`Cour`. With no season, nothing is read: the title-only answer stands."""
    return _season_pack_reading(title, season, titles)[0]


def parse_title(title: str, season: int | None = None, titles: Iterable[str] = ()) -> dict:
    """`season` is the entry's season (`entry_season`), or None, and `titles` the entry's titles. With a
    season, a season pack of that season is a pack (`season_pack`); `pack_by` says which pattern made the title a
    pack: `batch`, `range`, `season: <statement>`, or "" for none. Without it the answer is the title's words alone."""
    res = 0
    m = _RES.search(title)
    if m:
        token = m.group(1).lower()
        res = 2160 if token in ("2160p", "4k") else int(token[:-1])
    group = ""
    m = _GROUP_LEAD.search(title)
    if m:
        group = m.group(1)
    else:
        m = _GROUP_TRAIL.search(title)
        if m:
            group = m.group(1)
    multisub = bool(_MULTISUB.search(title))
    src = _SOURCE.search(title)
    source = (src.group(1) if src else "").upper()
    source = {"CRUNCHYROLL": "CR", "DISNEY": "DSNP", "DISNEY+": "DSNP", "NETFLIX": "NF", "AMAZON": "AMZN", "BILIBILI": "BILI",
              "IQIYI": "IQ", "BLURAY": "BD", "B-GLOBAL": "BGLOBAL", "BGLOBAL": "BGLOBAL"}.get(source, source)
    m = pack_range_search(title)
    pack_by = ("batch" if m.group(0).lower() == "batch" else "range") if m else ""
    refused = ""
    if not pack_by and season is not None:
        statement, refused = _season_pack_reading(title, season, titles)
        pack_by = f"season: {statement}" if statement else ""
    return {
        "group": group,
        "group_key": group.lower(),
        "source": source,
        # Learned-stats key: the same group ships different subtitle sets per streaming source
        # (Kumo-raws CR rips carry Arabic, their DSNP rips do not — observed 2026-09-15).
        "stats_key": f"{group.lower()}|{source}" if group else "",
        "resolution": res,
        "hevc": bool(_HEVC.search(title)),
        "multisub": multisub,
        "cr_source": bool(_CR.search(title)),
        "dub_only": bool(_DUB.search(title)) and not multisub,
        "hardsub": bool(_HARDSUB.search(title)),
        "batch": bool(pack_by),
        "pack_by": pack_by,
        # Why a title stating the entry's season is not read as its pack, else "".
        "pack_refused": refused,
    }


# The subtitle statements release names use besides a language's name, read as that language's subtitles,
# each counted over 2,890 searched names (the 3,256 of a first count also held the names the search
# rejected).
_SUB_WORD = r"sub(?:s|titles?)?"                     # Sub, Subs, Subtitle(s)
_VOSTFR = r"vostfr"                                  # French subtitles; the dub reading's subtitle words use it too
_SUB_STATEMENTS = {
    "eng": (r"eng(?:lish)?\.?[\s._-]*sub(?:s|bed|titles?)?", r"engsub(?:bed|s)?"),
    "spa": (r"espa[ñn]ol",),
    "por": (r"pt-br", r"por-br"),
    "fre": (_VOSTFR, r"subfrench"),
    "ger": (r"ger(?:man)?[\s._-]*subs?",),
    "pol": (r"napisy",),
    # The added languages' statements, counted over the same 2,890 searched names (2026-10-04).
    "ind": (r"sub[\s._-]*indo",),
    "vie": (r"vietsub",),
}


def _whole(alt: str) -> str:
    """`alt` as a whole word: no letter or digit on either side (`_` and punctuation end it), unlike `\b`, which a
    run of CJK letters or an `_` does not end."""
    return rf"(?<![^\W_])(?:{alt})(?![^\W_])"


# Chinese statements in Chinese: a run of 简/繁 with the words around them (`[简繁字幕]`, `[简繁外挂字幕]`,
# `[简繁日双语外挂]`), or 中文字幕, and `BIG5`. Read without `\b`: a run of CJK letters is one `\w` run.
_CHI_STATEMENT = _whole(r"[简繁][简繁体體中日英双雙语語文内內外挂掛封嵌字幕]*|中文字幕") + r"|\bbig5\b"
# A subtitle word followed by a list of codes states each of them (`sub ITA ENG`, `Subs.Ita.Eng.Jpn`, `Subs ENG-ITA`,
# `Sub. Español`); a language's own three-letter codes end its statement, and these words too.
# Every table language's three-letter codes, so `Subs.Eng.Tha` reads on past `Tha`; `esp` and
# `jap` are the spellings names use besides them.
_SUB_LIST_CODES = tuple(sorted({a for code, (aliases, _) in LANGUAGE_TABLE.items() for a in (code, *aliases)
                                if len(a) == 3 and a.isalpha()} | {"esp", "jap"}))
_SUB_LIST = _SUB_WORD + r"\.?[\s._-]*(?:(?:" + "|".join(_SUB_LIST_CODES) + r")[\s._,-]+)*"
_SUB_LIST_WORDS = {"eng": r"english", "spa": r"esp(?:a[ñn]ol)?"}

# A source (the streaming platform a release was ripped from) whose releases carry its market's
# subtitle language, read as that language's statement (`… Shahid.WEB-DL …` as Arabic, the way `[Sub Esp]` is
# Spanish). One row per source: its name, the language, the words names write it with (whole words, any case). Any
# language may have rows. The rule, measured 2026-10-04:
# - the language is the source's own market, a public fact cited on the row (track lists cannot choose it);
# - the stored lists agree: it is on every distinct release with the word whose track list is known;
# - the source is not multi-language: the median languages per known list is 2 or fewer (CR 8, NF 14.5, AMZN 6,
#   DSNP 6, B-Global 3.5, BILI 3, iQIYI 6, HMAX 14, Hulu 8 state none).
# The cut was set on n = 1 and 2: a cut of 3 admits BILI → English; the upper middle value drops HIDIVE (1 and 3).
# A row says the language is there, not that the others are not (Shahid's one read release carries English too), so it
# is not a `stated_languages` reading and gives no `subs.stated_other`.
SOURCE_LANGUAGES: dict[str, tuple[str, tuple[str, ...]]] = {
    # MBC Shahid, "the most-watched Arabic streaming service in the Middle East and North Africa" (Wikipedia):
    "Shahid": ("ara", ("shahid",)),          # 1 of 1 known list (Arabic, English); 2 names of 5,420
    # Hidive, "an American subscription video on demand … streaming service" (Wikipedia):
    "HIDIVE": ("eng", ("hidive", "hidi")),   # 2 of 2 known lists (English; Spanish, Portuguese on 1); 4 names
}
_SOURCE_WORDS = {svc: re.compile(_whole("|".join(map(re.escape, words))), re.I)
                 for svc, (_, words) in SOURCE_LANGUAGES.items()}
_SOURCE_ALT = {lang: "|".join(re.escape(w) for l, words in SOURCE_LANGUAGES.values() if l == lang for w in words)
               for lang, _ in SOURCE_LANGUAGES.values()}


# A language code that release names also write for something else is read as its tag only in capitals:
# `[FIN]` is Finnish in a list of tags (8 of 2,890 searched names), `[Fin]` closes a finished series (2).
_CAPITAL_TAGS = frozenset({"fin"})


@functools.lru_cache(maxsize=64)
def language_title_regex(lang: str, statements: bool = True, sources: bool = True) -> re.Pattern:
    """`statements=False` reads the language's names and `[ARA]`-style tags only, without the statement words: the
    pre-score's reading (`score_candidate`). `SOURCE_LANGUAGES`' words read in both, as a language's name does (the
    pre-score gives them +25, a multi-sub mark's worth); `sources=False` leaves them out (`stated_languages`)."""
    codes, words = LANGUAGE_TABLE.get(lang, ((lang,), (lang,)))
    code_alt = "|".join(rf"(?-i:{c.upper()})" if c in _CAPITAL_TAGS else re.escape(c) for c in codes if len(c) == 3)
    listed = "|".join([re.escape(c) for c in codes if len(c) == 3] + ([_SUB_LIST_WORDS[lang]] if lang in _SUB_LIST_WORDS else []))
    word_alt = "|".join([re.escape(w) for w in words if len(w) > 3]
                        + (list(_SUB_STATEMENTS.get(lang, ())) + ([rf"{_SUB_LIST}(?:{listed})"] if listed else [])
                           if statements else []))
    parts = []
    if code_alt:
        parts.append(rf"(?P<code>\[(?:{code_alt})\])")          # [ARA]; `title_language` reads the group name
    if word_alt:
        parts.append(rf"(?P<word>\b(?:{word_alt})\b)")          # Arabic, VOSTFR, Sub Esp
    if statements and lang == "chi":
        parts.append(rf"(?P<cjk>{_CHI_STATEMENT})")             # [简繁字幕]
    if sources and lang in _SOURCE_ALT:
        parts.append(rf"(?P<source>{_whole(_SOURCE_ALT[lang])})")            # Shahid
    return re.compile("|".join(parts), re.I)


# A language named with a dub is the audio track, not the subtitles (`[Arabic Dub]`,
# `(Japanese, English Dubs)`). The words are the ones the logged release names use; measured on
# 2026-09-30. Each word list is written once and every pattern below is built from it.
_DUB_WORDS = r"dub(?:s|bed|bing)?"
_DUB_AUDIO_WORDS = rf"(?:{_DUB_WORDS}|audio|dublado|vf)"
_DUB_SUB_WORDS = rf"(?:multi[\s._-]?)?(?:sub(?:s|bed|titles?|titled)?|{_VOSTFR})"
_EDGE_L, _EDGE_R = r"(?<![^\W_])", r"(?![^\W_])"
_DUB_AUDIO_WORD = re.compile(_EDGE_L + _DUB_AUDIO_WORDS + _EDGE_R, re.I)
_DUB_SUB_WORD = re.compile(_EDGE_L + _DUB_SUB_WORDS + _EDGE_R, re.I)
_DUB_AFTER = re.compile(r"[\s._-]*" + _DUB_WORDS + _EDGE_R, re.I)
_DUB_BEFORE = re.compile(_EDGE_L + _DUB_WORDS + r"[\s._-]*$", re.I)
_DUB_BEFORE_HEAD = len("dubbing")      # its longest word, before the run of separators (`_before_window`)
_DUB_SUB_AFTER = re.compile(r"[\s._-]*" + _DUB_SUB_WORDS + _EDGE_R, re.I)
_MULTI_AUDIO = re.compile(r"\bmulti[\s._-]?(?:audio|" + _DUB_WORDS + r")\b", re.I)
_OPENERS, _CLOSERS = "[({【", "])}】"
_CLAUSE_CUT = re.compile(r"[+&|/;]")

# Which releases are dubs, for the `dubs` setting. Separate from `parse_title`'s `dub_only`, which the score's
# `pen.dub_only` reads and `dubs=allow` keeps exactly as it was.
# AniList's `countryOfOrigin` -> the show's original audio: the language tags a container may carry for it (ffprobe
# reports MKV's ISO 639-2 tag, MP4's may be 639-1), and the words a release name uses for it.
_ORIGINAL_JA = (("jpn", "ja", "jp"), ("japanese", "jap", "jpn", "jp"))
_ORIGINAL_ZH = (("chi", "zho", "zh", "cmn", "yue"), ("chinese", "mandarin", "cantonese", "chi", "zho"))
ORIGINAL_AUDIO = {"JP": _ORIGINAL_JA, "KR": (("kor", "ko"), ("korean", "kor")), "CN": _ORIGINAL_ZH, "TW": _ORIGINAL_ZH}
# The dub words as a word of their own (so a group whose name ends in `Dubs` states nothing), or glued to a
# language code (`GerDub`); `dublado` is Portuguese for dubbed. `VF` is not read: in the 4,020 recorded names it never
# stands without `VOSTFR` or `MULTi` beside it (the French audio as one of two).
_DUB_CODES = "|".join(sorted({a for aliases, _ in LANGUAGE_TABLE.values() for a in aliases
                              if len(a) == 3 and a.isalpha()}))
_DUB_STATEMENT = re.compile(_EDGE_L + rf"(?:(?:{_DUB_CODES}))?(?:{_DUB_WORDS}|dublado)" + _EDGE_R, re.I)
# Audio in more than one language: the original is one of them, so the release is no dub. A bare `DUAL` is the
# scene's dual audio (`[WEB-DL CR 1080p x264 AAC][DUAL][PT-BR] (… | Dublado)`, five recorded names).
_DUB_MULTI = re.compile(_EDGE_L + r"(?:(?:dual|multi(?:ple)?|triple|tri)[\s._-]?(?:audios?|dubs?)|dual)" + _EDGE_R,
                        re.I)


def original_audio(country) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """(language tags, title words) of a show's original audio from its AniList `countryOfOrigin`; None when the
    country is unknown or not one of `ORIGINAL_AUDIO`'s (the audio reading then says nothing)."""
    return ORIGINAL_AUDIO.get(str(country or "").strip().upper())


# A name's leading bracket is its group tag (`[Dub-Raws]`), unless every word in it is about the audio, the subtitles
# or a language (`[Dual Audio]`, `[DUAL]`, `[English Dub]`, `[JPN+ENG]`): then it is read as the statement it is.
_DUB_GROUP_TAG = re.compile(r"^\s*[\[【]([^\]】]*)[\]】]")
# A name's words, a glued one in its capitalised parts (`GerJapEng` → Ger, Jap, Eng; `JPBD` stays one word).
_DUB_TOKEN = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_DUB_LANGUAGE_WORDS = frozenset(
    {w for aliases, names in LANGUAGE_TABLE.values() for w in (*aliases, *names)}
    | {w for tags, words in ORIGINAL_AUDIO.values() for w in (*tags, *words)} | {"latino", "castellano"})
_DUB_AUDIO_TOKENS = frozenset({"audio", "audios", "dual", "multi", "triple", "tri", "dub", "dubs", "dubbed", "dubbing",
                               "dublado"})
_DUB_SUB_TOKENS = frozenset({"sub", "subs", "subbed", "subtitle", "subtitles", "subtitled", "multisub", "multisubs",
                             "vostfr", "softsub", "softsubs", "hardsub", "hardsubs", "soft", "hard", "forced", "signs",
                             "songs", "matching", "multiple"})


def _dub_tokens(text: str) -> list[re.Match]:
    return list(_DUB_TOKEN.finditer(text))


# The words that make a `Sub` beside a dub the dub's own subtitles, not a second, original audio: `Soft Subs`,
# `Matching Subs` (subtitles matching the dub), `Forced`, `Signs & Songs`, `Hard`, and a multi-subtitle mark (`Multi
# Subs`, `Multiple Subs`, `Multi-Sub`, `MultiSubs`, one reading for all).
_DUB_SUB_QUALIFIERS = frozenset({"soft", "matching", "forced", "signs", "songs", "hard", "closed", "multi", "multiple"})
_DUB_OPEN_CLOSE = re.compile(r"[\[\](){}【】]")


def _offers_subs(text: str) -> bool:
    """Whether `text` names a subtitle beside the dub as a second offer: a bare `Sub`/`Subs`/`Subbed` (`Sub/Dub`,
    `sub dub ita`) or one tagged with a language (`Eng Sub`, `Sub ita`, `EngSub`). One a qualifier word leads
    (`_DUB_SUB_QUALIFIERS`) is the dub's own subtitles; one glued to the word after it (`SubsHaven`) is no subtitle."""
    tokens = _dub_tokens(text)
    for i, tok in enumerate(tokens):
        if tok.group(0).lower() not in ("sub", "subs", "subbed"):
            continue
        if tok.end() < len(text) and text[tok.end()].isalpha():
            continue                          # glued to the next word: a name, not a subtitle
        if i and tokens[i - 1].group(0).lower() in _DUB_SUB_QUALIFIERS and \
                not _DUB_OPEN_CLOSE.search(text[tokens[i - 1].end():tok.start()]):
            continue
        return True
    return False


def _states_audio_or_language(text: str) -> bool:
    """Whether every word of `text` is about the audio, the subtitles or a language (a bracket that is a statement)."""
    words = [m.group(0).lower() for m in _dub_tokens(text)]
    return bool(words) and all(w in _DUB_LANGUAGE_WORDS or w in _DUB_AUDIO_TOKENS or w in _DUB_SUB_TOKENS
                               for w in words)


def _blank(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


# The words beside which a language is the audio's (`Japanese Audio`, `JP Dual`, `JPN Dub`).
_DUB_BESIDE_AUDIO = frozenset({"audio", "audios", "dual", "dub", "dubs", "dubbed", "dubbing", "dublado"})
# What may stand between two words of one statement (`JPN+ENG`, `JP/EN`, `Japanese, English`, `GerJapEng`).
_DUB_LIST_GAP = re.compile(r"[\s+/,&._-]*")


def _original_as_audio(reading: str, own: frozenset) -> bool:
    """Whether `reading` names the original language (a word of `own`) as audio: in a dub's own bracket
    (`[Japanese + English Dub]`, `GerJapEng-Dub`), beside an audio word (`[Japanese Audio]`, `JPN Dub`), or in a list of
    languages (`[JPN+ENG]`, `[JP/EN]`, `Japanese, English`). Named for something else it is not: `Japanese songs`,
    `JP BD`, `JPBD` (one word), `Japan`, `Chihiro`."""
    uncut = _CLAUSE_CUT.sub(",", reading)
    brackets = _brackets(uncut)
    for m in _DUB_STATEMENT.finditer(reading):
        span = brackets.clause(m.start(), m.end())
        if span is not None and brackets.once(("own", own, *span), lambda: any(
                t.group(0).lower() in own for t in _dub_tokens(uncut[span[0]:span[1]]))):
            return True
    tokens = _dub_tokens(reading)
    for i, tok in enumerate(tokens):
        if tok.group(0).lower() not in own:
            continue
        for j in (i - 1, i + 1):
            if not 0 <= j < len(tokens):
                continue
            other = tokens[j]
            gap = reading[other.end():tok.start()] if j < i else reading[tok.end():other.start()]
            word = other.group(0).lower()
            if _DUB_LIST_GAP.fullmatch(gap) and (word in _DUB_BESIDE_AUDIO or word in _DUB_LANGUAGE_WORDS):
                return True
    return False


def dub_reading(title: str, country=None, titles=()) -> str | None:
    """The words that make a release name a dub (`[English Dub]`, `Eng Dub`, `GerDub`, `Dublado`, `[DUB]`), or None.

    Read on the name without its group tag (the leading bracket, unless it is itself a statement: `[Dual Audio]`,
    `[English Dub]`) and without the show's own `titles` (a show named with the word, `Rub-a-Dub Club`). No dub when
    the name states audio in more than one language (`Dual Audio`, `Multi-Audio`, `Multi-Dub`, `Triple Audio`,
    `DUAL`), when it names the show's original language as audio (in the dub's bracket, beside an audio word or in a
    list of languages: `[Japanese Audio][English Dub]`, `[JP/EN][Dub]`, `GerJapEng-Dub`; not `Japanese songs` or
    `JP BD`; Japanese when the country is unknown, which the anime this plugin plays nearly always are), or
    when it names a bare or language-tagged subtitle beside the dub (`Sub/Dub`, `[Eng Dub + Eng Sub]`, `sub dub ita`):
    such a release offers both. A qualified one does not undo it (`[Multi-Subs]`, `[Multiple Subs]`, `(Matching Soft
    Subs)`, `[Forced Subs]`, `[Signs & Songs Subs]`: the dub's own subtitles)."""
    title = str(title or "")
    reading = title
    tag = _DUB_GROUP_TAG.match(reading)
    if tag and not _states_audio_or_language(tag.group(1)):
        reading = _blank(reading, 0, tag.end())
    for own_title in titles or ():
        words = [w for w in re.split(r"[\W_]+", str(own_title or "")) if w]
        if len("".join(words)) < 3:
            continue
        for m in re.finditer(r"[\W_]+".join(map(re.escape, words)), reading, re.I):
            reading = _blank(reading, m.start(), m.end())
    if _DUB_MULTI.search(reading) or _offers_subs(reading):
        return None
    if _original_as_audio(reading, frozenset((original_audio(country) or _ORIGINAL_JA)[1])):
        return None
    for m in _DUB_STATEMENT.finditer(reading):
        clause = _bracket_clause(title, m.start(), m.end())
        if clause is not None:
            return clause[0].strip()
        before = re.search(r"([^\W_]+)[\s._-]+$", reading[:m.start()])
        return ((before.group(1) + " ") if before else "") + m.group(0)
    return None


def audio_dub(audio, country) -> bool | None:
    """Whether probed audio makes a release a dub: True when every track is tagged, none is in the show's original
    language and not every one is English; False when one is in the original language; None otherwise (the country or
    a track's language unknown, `und`, untagged). English alone is no evidence: a Matroska audio track without a
    Language element reads as `eng` (its default; an old mux of the original audio shows so), so an English-only probe
    leaves the name to decide. A track in another language (`spa`, `ara`, `ita` on a Japanese show) still marks it."""
    original = original_audio(country)
    tags = [str(a or "").strip().lower().replace("_", "-") for a in audio or []]
    if original is None or not tags or any(t in ("", "und") for t in tags):
        return None
    if any(t in original[0] or t.split("-")[0] in original[0] for t in tags):
        return False
    return None if all(t.split("-")[0] in ("eng", "en") for t in tags) else True


class _Brackets:
    """One name's brackets, `+ & | / ;` cuts and commas, read once (a scan per language-word match was
    quadratic, minutes for one long name). `left[s]` is where a scan back from `s - 1` finds the innermost opener
    around `s` (None when none), `right[e]` where a scan on from `e` finds its closer (the name's length when none);
    `prev_*[i]` is the last cut or comma before `i` (-1), `next_*[i]` the first at or after `i` (the length). `once`
    keeps a reading of one span, so the matches of one clause read it once."""

    __slots__ = ("title", "left", "right", "prev_cut", "next_cut", "prev_comma", "next_comma", "_memo")

    def __init__(self, title: str):
        n = len(title)
        self.title, self._memo = title, {}
        self.left: list = [None] * (n + 1)
        for s in range(1, n + 1):
            ch, before = title[s - 1], self.left[s - 1]
            if ch in _OPENERS:
                self.left[s] = s - 1
            elif ch in _CLOSERS:              # the opener that closes it, then the one around that
                self.left[s] = self.left[before] if before is not None else None
            else:
                self.left[s] = before
        self.right: list = [n] * (n + 1)
        for e in range(n - 1, -1, -1):
            ch, after = title[e], self.right[e + 1]
            if ch in _CLOSERS:
                self.right[e] = e
            elif ch in _OPENERS:
                self.right[e] = self.right[after + 1] if after < n else n
            else:
                self.right[e] = after
        self.prev_cut, self.next_cut = self._marks(title, lambda ch: _CLAUSE_CUT.fullmatch(ch) is not None)
        self.prev_comma, self.next_comma = self._marks(title, lambda ch: ch == ",")

    @staticmethod
    def _marks(title: str, is_mark) -> tuple[list, list]:
        n = len(title)
        prev, last = [-1] * (n + 1), -1
        for i in range(n):
            prev[i] = last
            if is_mark(title[i]):
                last = i
        prev[n] = last
        nxt, first = [n] * (n + 1), n
        for i in range(n - 1, -1, -1):
            if is_mark(title[i]):
                first = i
            nxt[i] = first
        return prev, nxt

    def clause(self, start: int, end: int) -> tuple[int, int] | None:
        """`_bracket_clause`'s part as a span of the name."""
        opener = self.left[start]
        if opener is None:
            return None
        left, right = opener + 1, self.right[end]
        cut = self.prev_cut[start]
        return max(left, cut + 1), min(right, self.next_cut[end])

    def once(self, key, read):
        if key not in self._memo:
            self._memo[key] = read()
        return self._memo[key]


@functools.lru_cache(maxsize=256)
def _brackets(title: str) -> _Brackets:
    return _Brackets(title)


def _bracket_clause(title: str, start: int, end: int) -> tuple[str, int, int] | None:
    """The innermost bracket around title[start:end], cut at `+ & | / ;` to the part that holds it
    (`[Arabic Dub + English Subs]` → `Arabic Dub `), with the match's offsets in that part. None when no bracket
    holds it."""
    span = _brackets(title).clause(start, end)
    if span is None:
        return None
    lo, hi = span
    return title[lo:hi], start - lo, end - lo


def _clause_audio(brackets: _Brackets, lo: int, hi: int, start: int, end: int) -> str | None:
    """The part that makes the match title[start:end] the audio, or None, in its clause title[lo:hi]. The match's
    own comma segment decides first (`[Arabic Dub, English Subs]`: `Arabic Dub` is the audio); a segment with neither
    kind of word falls back to the whole clause, so a list keeps its heading (`[Dub - Japanese, English, Arabic]`,
    `(English, Arabic Dubs)`) and `(VF, FRENCH, VOSTFR)` stays subtitles. Each span is read once per name."""
    title = brackets.title
    seg_lo, seg_hi = max(lo, brackets.prev_comma[start] + 1), min(hi, brackets.next_comma[end])

    def reading(a: int, b: int) -> tuple[bool, bool, str]:
        part = title[a:b]
        return bool(_DUB_SUB_WORD.search(part)), bool(_DUB_AUDIO_WORD.search(part)), part.strip()
    sub, audio, segment = brackets.once((seg_lo, seg_hi), lambda: reading(seg_lo, seg_hi))
    if sub:
        return None
    if audio:
        return segment
    sub, audio, clause = brackets.once((lo, hi), lambda: reading(lo, hi))
    return clause if audio and not sub else None


@functools.lru_cache(maxsize=8192)
def title_language(title: str, lang: str, statements: bool = True,
                   sources: bool = True) -> tuple[bool, tuple[str, ...]]:
    """(the release name names `lang` as a subtitle language, the parts that named it as the audio instead).

    A match is the audio when its clause (`_bracket_clause`, then `_clause_audio`) says so: `[Arabic Dub]`,
    `(Dub Arabic)`, `(English, Japanese, French Dubs)` and `[Arabic Dub, English Subs]` (for Arabic) are audio;
    `[Arabic Subs]`, `[Arabic Dub + Arabic Subs]` and `[Sub - English, Arabic]` are subtitles. Outside brackets a
    match is the audio when a dub word touches it (`Arabic Dubbed`, `Arabic-Dub`) and no subtitle word follows it.
    A code tag (`[ARA]`) is a clause of its own. The name counts when any match is read as subtitles. Cached: the
    pre-score, the score, its log line and the file-pick rescore all ask about the same names. `statements=False`
    leaves out the statement words, `sources=False` the source rows (`language_title_regex`).

    A source row's word counts only when no match of `lang` anywhere in the name is the audio: a name
    whose own words say the language is the dub (`… Shahid WEB-DL (Arabic Dub)`, `[Shahid Arabic Dub]`) states no
    subtitles, whatever the source ships. Its own dub clause (`[Shahid Dub]`) makes it the audio too."""
    named, by_source, audio, source_audio = False, False, [], []
    for m in language_title_regex(lang, statements, sources).finditer(title):
        if m.groupdict().get("code"):
            named = True
            continue
        part = _match_audio(title, m)
        if m.groupdict().get("source"):
            if part:
                source_audio.append(part)
            else:
                by_source = True
            continue
        if part:
            audio.append(part)
            continue
        named = True
    audio += [p for p in source_audio if p not in audio]
    return named or (by_source and not audio), tuple(audio)


def _match_audio(title: str, m: re.Match) -> str | None:
    """The part that makes match `m` the audio (`title_language`'s rule), or None: its clause when a bracket holds it,
    else a dub word touching it with no subtitle word after it."""
    brackets = _brackets(title)
    span = brackets.clause(m.start(), m.end())
    if span is not None:
        return _clause_audio(brackets, *span, m.start(), m.end())
    after = _DUB_AFTER.match(title, m.end())
    before = _DUB_BEFORE.search(title, _before_window(title, m.start(), "._-", _DUB_BEFORE_HEAD), m.start())
    if (after or before) and not _DUB_SUB_AFTER.match(title, m.end()):
        return title[before.start() if before else m.start(): after.end() if after else m.end()]
    return None


def title_language_trace(title: str, lang: str) -> dict:
    """The `score candidate` fields for a name whose language was read, at least once, as a dub (empty otherwise):
    the parts read as the audio and why the bonus went or stayed."""
    named, audio = title_language(title, lang)
    if not audio:
        return {}
    why = (f"{lang} is also named as subtitles elsewhere in the name: subs.title_lang kept" if named else
           f"{lang} is named only with a dub word ({audio[0]}): the audio, not subtitles; no subs.title_lang")
    return {"title_lang_audio": list(audio), "title_lang_reason": why}


def source_trace(title: str, lang: str) -> dict:
    """The `score candidate` fields for a name carrying a `SOURCE_LANGUAGES` word (empty otherwise):
    each such source and the language it states; `title_lang_source`, the source, when the pre-score's reading of
    `lang` rests on the row alone (why the release was read early: no per-candidate pre-score is logged), and
    `title_lang_source_final` when the final score's does too (its `subs.title_lang` when its tracks were not read).
    A statement word with the row (`… Shahid WEB-DL [Subs.Ara.Eng]`) is read only in the final score."""
    found = {svc: SOURCE_LANGUAGES[svc][0] for svc, rx in _SOURCE_WORDS.items() if rx.search(title)}
    if not found:
        return {}
    out: dict = {"source_langs": found}
    if title_language(title, lang, False)[0] and not title_language(title, lang, False, False)[0]:
        out["title_lang_source"] = ", ".join(svc for svc, l in found.items() if l == lang)
        if not title_language(title, lang, sources=False)[0]:
            out["title_lang_source_final"] = True
    return out


def has_language(langs: list[str] | None, lang: str) -> bool | None:
    """A track list holds `lang`, in any region or script (`ar-SA`, `pt-BR`: mpv's tags in `tracks.json`)."""
    if langs is None:
        return None
    return any(same_language(l, lang) for l in langs)


# The language-neutral fallback. The two points are no setting: edit the source to change them. The second language
# itself is the viewer's `subtitleFallbackLanguage` setting (English by default, or `off`).
FALLBACK_LANG_POINTS = 15        # its term, `subs.fallback_lang`: what a multi-sub mark is worth
STATED_OTHER_POINTS = -40        # `subs.stated_other`: a name stating only other subtitle languages, as a read list


# The languages whose statements count for `subs.stated_other`: every table language (language neutrality
# first). A name that states only Chinese (`[简繁字幕]`, `[CHT]`, often burned in), only
# Greek (`GREEK SUBS`) or only Malay is marked down for every other viewer, as `[Sub Esp]` is. Measured on
# 49 valid episodes: no pick of the eleven moves; terms and top tens do.
STATED_LANGUAGES = tuple(LANGUAGE_TABLE)


@functools.lru_cache(maxsize=4096)
def stated_languages(title: str) -> tuple[str, ...]:
    """The `STATED_LANGUAGES` a release name states as subtitles (`title_language`'s reading: a dub's language is not
    one, nor a source row's: a source word says its language is there, not that the others are not)."""
    return tuple(l for l in STATED_LANGUAGES if title_language(title, l, sources=False)[0])


def stated_other_reading(title: str, lang: str, second: str | None = None) -> tuple[tuple[str, ...], str]:
    """(the languages a name states when it states subtitle languages, none of them `lang` nor the fallback's
    `second`, and no multi-subtitle mark; why not, when it states some: `wanted`, `second` or `multisub`). Such a
    name says what its subtitles are, as a read list would."""
    stated = stated_languages(title)
    if not stated:
        return (), ""
    if title_language(title, lang)[0]:
        return (), "wanted"
    if second and (second in stated or title_language(title, second)[0]):     # a row's too
        return (), "second"
    if multisub_mark(title):
        return (), "multisub"
    return stated, ""


def stated_other(title: str, lang: str, second: str | None = None) -> tuple[str, ...]:
    """`stated_other_reading`'s languages: () unless the name states only other subtitle languages."""
    return stated_other_reading(title, lang, second)[0]


def stated_trace(title: str, lang: str, fallback: dict | None, verified: bool | None, terms: dict) -> dict:
    """The `score candidate` fields for a name that states subtitle languages (empty otherwise): the stated
    languages, and either the `subs.stated_other` term's languages or why the name got none: `read` (a read list
    decides outside the fallback), `wanted`, `second`, `multisub`, or `points` (`STATED_OTHER_POINTS` is 0)."""
    stated = stated_languages(title)
    if not stated:
        return {}
    if "subs.stated_other" in terms:
        return {"stated_langs": list(stated), "stated_other": list(stated)}
    langs, skip = stated_other_reading(title, lang, (fallback or {}).get("second"))
    if verified is not None and not (fallback and verified is False):
        skip = "read"
    return {"stated_langs": list(stated), "stated_other_skip": skip or "points"}


def language_fallback(cands: list[dict], lang: str, fallback_lang: str | None) -> dict:
    """Whether no candidate is known to carry `lang` (no read list has it, no name states it). Then the
    subtitle terms stop depending on which releases were read (`score_candidate`'s `lang_fallback`). A name counts only
    for a release whose tracks are unknown: a read list overrides its own name. `propagated` counts the lists
    `propagate_verification` copied from a sibling, apart from the ones read. `fallback_lang` is the
    viewer's second language (`core.fallback_language`), None for `off`; it is `second` unless it is `lang`."""
    read = [c for c in cands if c.get("verified_langs") is not None]
    propagated = sum(1 for c in read if c.get("inferred_from_sibling"))
    with_lang = sum(1 for c in read if has_language(c["verified_langs"], lang))
    named = sum(1 for c in cands if c.get("verified_langs") is None and title_language(c.get("title") or "", lang)[0])
    applies = bool(cands) and not with_lang and not named
    second = fallback_lang if applies and fallback_lang and not has_language([lang], fallback_lang) else None
    stated: dict[str, int] = {}
    for c in cands:
        for l in stated_other(c.get("title") or "", lang, second):
            stated[l] = stated.get(l, 0) + 1
    if applies:
        why = (f"no release has {lang}: {len(read) - propagated} track lists read and {propagated} copied from a "
               f"sibling without it, {len(cands) - len(read)} unread, no unread name states it")
    else:
        why = f"{lang} is on {with_lang} known track lists and named by {named} unread release names"
    return {"applies": applies, "reason": why, "read": len(read) - propagated, "propagated": propagated,
            "unread": len(cands) - len(read),
            "with_lang": with_lang, "named": named, "stated_other": dict(sorted(stated.items())), "second": second}


def score_candidate(c: dict, parsed: dict, lang: str, now: float | None = None,
                    want_batch: bool = False, preferred_resolution: int = 1080,
                    group_stats: dict | None = None, static_priors: dict | None = None, *,
                    stage: str) -> tuple[float, dict]:
    """The release score. Returns (total, terms) with every term logged by the caller.

    `stage` is "pre" or "final", and every caller says which:
    - "pre", `rank_candidates`' pre-score, only orders the lazy track reads. It reads names without the statement
      words and gives no `subs.stated_other`, so a `VOSTFR` or `Napisy PL` name is not read ahead of a better-seeded
      multi-sub release that carries the language too; a read list without the language keeps its -40.
    - "final", `rank_candidates`' score and `cli.pick_pack_files`' rescore: the statement words, `subs.stated_other`,
      and the fallback `c["lang_fallback"]` carries (None or missing: not in the fallback)."""
    if stage not in ("pre", "final"):
        raise ValueError(f"score_candidate: stage must be 'pre' or 'final', not {stage!r}")
    final = stage == "final"
    now = now or time.time()
    terms: dict[str, float] = {}
    verified = has_language(c.get("verified_langs"), lang)
    title_lang, dub_parts = title_language(c.get("title", ""), lang, final)       # not a dub's language
    # A dub's `Multi Audio`/`Multi-Dub` is not a multi-subtitle mark (`_MULTISUB`'s bare `multi`).
    multi_is_audio = bool(dub_parts) and not multisub_mark(c.get("title", ""))
    multisub = parsed["multisub"] and not multi_is_audio
    # `rank_candidates` sets `lang_fallback` on every candidate once its reads are done (None: some release is
    # known to carry the language); only the final score reads it.
    fallback = c.get("lang_fallback") if final else None
    second = (fallback or {}).get("second")
    other = stated_other(c.get("title", ""), lang, second) if final and (verified is None or fallback) else ()

    # Subtitles (dominant). In the fallback a read list without the language is scored by its name, as an unread
    # release is, so reading a release never moves it down.
    if verified is True:
        terms["subs.verified"] = 60
        if c.get("default_track"):
            terms["subs.default"] = 2
    elif verified is False and not fallback:
        terms["subs.verified_absent"] = -40
    elif other and STATED_OTHER_POINTS:
        terms["subs.stated_other"] = STATED_OTHER_POINTS
    elif title_lang:
        # A name the pre-score reads only through a source row is worth a CR multi-sub mark there (+25),
        # so a better-seeded multi-sub release that may carry the language is still read first.
        terms["subs.title_lang"] = 45 if final or title_language(c.get("title", ""), lang, False, False)[0] else 25
    elif multisub and parsed["cr_source"]:
        terms["subs.multisub_cr"] = 25
    elif multisub:
        terms["subs.multisub"] = 15
    if second and has_language(c.get("verified_langs"), second):
        terms["subs.fallback_lang"] = FALLBACK_LANG_POINTS
    if verified is None or fallback:
        # In the fallback every read group learns a miss, so a learned prior below 0 would bring the
        # slide back from a group's second read on; it counts as 0 there.
        prior = group_prior(parsed.get("stats_key") or parsed["group_key"], lang, group_stats,
                            static_key=parsed["group_key"], static=static_priors, floor_learned=bool(fallback))
        if prior:
            terms["subs.group_prior"] = prior

    # Quality
    res = parsed["resolution"]
    if res == preferred_resolution:
        terms["quality.resolution"] = 15
    elif res == 2160:
        terms["quality.resolution"] = 10
    elif res == 720:
        terms["quality.resolution"] = 5
    elif res == 480:
        terms["quality.resolution"] = -10
    if parsed["hevc"] and res == 1080:
        terms["quality.hevc"] = 3

    # Availability
    seeders = c.get("seeders")
    seeders = int(seeders) if isinstance(seeders, (int, float)) and seeders >= 0 else 0
    terms["avail.seeders"] = min(24.0, 8 * math.log10(seeders + 1))
    if seeders == 0:
        terms["avail.no_seeders"] = -30
    if c.get("rd_cached"):
        terms["avail.rd_cached"] = 20
    if c.get("trusted"):
        terms["avail.trusted"] = 5
    if c.get("remake"):
        terms["avail.remake"] = -5

    # Size per episode: the picked file when a pack has been inspected, else pack size / file count.
    files = max(int(c.get("num_files") or 1), 1)
    picked = c.get("file") or {}
    if picked.get("size"):
        size_ep = float(picked["size"])
    elif parsed.get("batch") and not c.get("num_files"):
        size_ep = 0          # a pack with no file count: unknown until a file list or a provider says
    else:
        size_ep = (c.get("total_size") or 0) / files
    if 250 * 2**20 <= size_ep <= 2.5 * 2**30:
        terms["size.sane"] = 5
    elif size_ep > 5 * 2**30:
        terms["size.remux"] = -15
    elif 0 < size_ep < 150 * 2**20:
        terms["size.tiny"] = -15

    # Penalties
    if parsed["dub_only"]:
        terms["pen.dub_only"] = -40
    if parsed["batch"] and not want_batch:
        terms["pen.batch"] = -8       # more bytes to fetch and a slower start; seeders decide the rest
    if parsed["hardsub"]:
        terms["pen.hardsub"] = -30
    if c.get("status") == "skipped":
        terms["pen.unverifiable"] = -5
    ts = c.get("timestamp") or now
    if now - ts > 30 * 86400 and seeders < 5:
        terms["pen.stale"] = -10

    return sum(terms.values()), terms


def parsed_of(cand: dict) -> str:
    """The `group|source` key a candidate's learned statistic belongs under."""
    parsed = cand.get("parsed") or parse_title(str(cand.get("title") or ""))
    return parsed.get("stats_key") or parsed.get("group_key") or ""


def release_shape(cand: dict) -> dict:
    """What a release's name states about the episodes it holds, for `propagate_verification`: a pack
    (`parsed`'s `batch`) with its season, or a single with its season and the episode numbers `title_matches_episode`
    reads. A season the name does not state is None, not 1. A pack's ranges are not compared: one group's packs of one
    season (`01 ~ 09`, `01 ~ 12`) carried the same list on every read of the measurement, and keying on the range
    played a 7-seeder pack over the 31-seeder one (measured 2026-10-04)."""
    title = str(cand.get("title") or "")
    parsed = cand.get("parsed") or parse_title(title)
    if parsed.get("batch"):
        return {"kind": "pack", "season": season_number(title)}
    return {"kind": "single", "season": season_number(title), "episodes": sorted(_episode_read(title)[0])}


def _shape_key(shape: dict) -> str:
    return json.dumps(shape, sort_keys=True)


def _mark_reading(title: str) -> str:
    """The text `multisub_mark` and `copy_key_mark` read: the name with its combining marks taken off
    (NFKD, then category `Mn` dropped), then its `Multi-Audio`/`Multi-Dub` blanked. So an audio word written with an
    accent, composed (`[Multi-Áudio]`) or as a letter and a combining mark, is the audio mark its plain spelling is, and
    its `Multi` is no bare `MULTi`. Both marks are read on this one text, so they cannot drift apart; it is never
    logged or returned, and nothing else reads it as the release's name."""
    folded = "".join(c for c in unicodedata.normalize("NFKD", str(title or "")) if unicodedata.category(c) != "Mn")
    return _MULTI_AUDIO.sub(" ", folded)


def multisub_mark(title: str) -> bool:
    """Whether a release name carries a multi-subtitle mark, the one reading `stated_other_reading` and
    `score_candidate`'s dub check share: `[MultiSub]`, `Multi-Subs`, `[Multiple Subtitle]` or a bare `MULTi`, but not a
    `Multi-Audio`/`Multi-Dub`, which says nothing of the subtitles. The score's `subs.multisub` still credits a lone
    `Multi-Audio`/`Multi-Dub` (one with no dub language). The copy key reads `copy_key_mark`: this reading without a
    bare `MULTi`. An accented audio word is one too (`[Multi-Áudio]`): the mark is read on `_mark_reading`'s folded
    name."""
    return bool(_MULTISUB.search(_mark_reading(title)))


def copy_key_mark(title: str) -> bool:
    """The multi-subtitle mark `propagate_verification` keys donors on: `multisub_mark`'s reading less a
    bare `MULTi`, the `_MULTISUB` match that is the word alone. A French group's `MULTi` states its audio (`MULTi … (VF,
    FRENCH, VOSTFR)`), and its twin without the word carried the same subtitles on every read of the measurement."""
    return any(m.group(0).lower() != "multi" for m in _MULTISUB.finditer(_mark_reading(title)))


PROPAGATE_REFUSED_LOGGED = 8


def propagate_verification(cands: list[dict], log: Log | None = None) -> int:
    """Same release group + same streaming source for the same episode ⇒ same subtitle tracks (only the
    resolution differs). Copy a verified track list onto unverified siblings so they are scored, not guessed,
    and so the probe loop does not spend a probe on a 720p twin of a release it already inspected.

    "The same episode" is the names' own statement (`release_shape`): a list is copied only between
    names of one `group|source` that state the same season (an unstated season is not season 1) and the same episode
    numbers, or the same pack shape. The season-1 `[Grp] Show - 05` lent its list to `[Grp] Show 2nd Season - 05`,
    another season's episode, and the copied +60 picked it.

    "The same tracks" also needs the same multi-subtitle mark (`copy_key_mark`) on both names. A group's
    release without the mark (`[Grp] Show - 11 [720p] [ENG]`, a CR WEB-DL without `[MultiSub]`) carries fewer
    subtitles than its `[MultiSub]` twin, and the copy gave it +60 or -40 for languages it does not carry. A bare
    `MULTi` is no mark here; it states the audio, and the twin without it carries the same subtitles.

    The `propagate` line samples each copy (`id`, `donor_id`, `mark`), so a trace shows which read name lent a list. A
    refused copy is logged (`propagate_refused`) whether or not anything was copied, with the names, shapes and marks
    on both sides and `why`: `shape` (no read name of that group, source and mark states the same episodes), `mark`
    (one states the same episodes but differs in the mark) or `shape+mark`. The would-be donor shown is the one the
    reason is about."""
    by_key: dict[tuple[str, str, bool], dict] = {}
    for c in cands:
        key = (c.get("parsed") or {}).get("stats_key") or ""
        if key and c.get("verified_langs") is not None:
            by_key.setdefault((key, _shape_key(release_shape(c)), copy_key_mark(c.get("title"))), c)
    copied, refused = [], []
    reasons = {"shape": 0, "mark": 0, "shape+mark": 0}
    for c in cands:
        key = (c.get("parsed") or {}).get("stats_key") or ""
        if not key or c.get("verified_langs") is not None:
            continue
        shape, mark = release_shape(c), copy_key_mark(c.get("title"))
        donor = by_key.get((key, _shape_key(shape), mark))
        if donor is not None:
            c["verified_langs"] = list(donor["verified_langs"])
            c["inferred_from_sibling"] = True
            copied.append({"id": c.get("id"), "donor_id": donor.get("id"), "mark": mark})
            continue
        group = [(s, m, d) for (k, s, m), d in by_key.items() if k == key]
        if not group:
            continue
        same_shape = [d for s, m, d in group if s == _shape_key(shape)]
        same_mark = [d for s, m, d in group if m == mark]
        why, other = (("mark", same_shape[0]) if same_shape else ("shape", same_mark[0]) if same_mark
                      else ("shape+mark", group[0][2]))
        reasons[why] += 1
        refused.append({"id": c.get("id"), "title": c.get("title"), "why": why, "shape": shape, "mark": mark,
                        "donor_id": other.get("id"), "donor": other.get("title"), "donor_shape": release_shape(other),
                        "donor_mark": copy_key_mark(other.get("title"))})
    if copied and log:
        log.info("verify", "propagate", result="PASS", siblings=len(copied), keys=sorted({k for k, _, _ in by_key}),
                 sample=copied[:PROPAGATE_REFUSED_LOGGED], more=max(0, len(copied) - PROPAGATE_REFUSED_LOGGED))
    if refused and log:
        log.info("verify", "propagate_refused", result="SKIP", refused=len(refused), reasons=reasons,
                 sample=refused[:PROPAGATE_REFUSED_LOGGED], more=max(0, len(refused) - PROPAGATE_REFUSED_LOGGED),
                 reason="same group and source, but the names state another season, episode or pack shape, "
                        "or one carries a multi-subtitle mark the other lacks: the verified list is not "
                        "copied")
    return len(copied)


# An episode range a pack names. `~` may be spaced (`01 ~ 12`, Kumo-raws); `-` may not, because a
# spaced ` - ` is the episode separator (`Batch 6 - 126-148` is 126-148, not 6-126). A range right after
# Part/Season/S/Vol/Batch/Cour numbers something else (`(Part 01-06)` is six Mimi parts).
_PACK_RANGE = re.compile(r"(?<![\w.])(\d{1,4})(?:\s*~\s*|-)(\d{1,4})(?![\w.]|\s*p\b)", re.I)
_PACK_RANGE_LABEL = re.compile(_RANGE_LABELS + r"\s*$", re.I)          # beside `_STATED_LABELS`
_PACK_RANGE_LABEL_HEAD = len("volume.")      # its longest label, before the run of whitespace (`_before_window`)


def pack_range_matches(title: str) -> list[re.Match]:
    """`pack_ranges`' matches, each the text of one episode range a release title names (`01-23`, `01 ~ 23`)."""
    out = []
    t = str(title or "")
    for m in _PACK_RANGE.finditer(t):
        lo, hi = int(m.group(1)), int(m.group(2))
        # From the label's window, not the whole name before it: searched from 0 per range, quadratic on a long name.
        if lo > hi or hi >= 1900 or _PACK_RANGE_LABEL.search(
                t, _before_window(t, m.start(), "", _PACK_RANGE_LABEL_HEAD), m.start()):
            continue
        out.append(m)
    return out


def pack_ranges(title: str) -> list[tuple[int, int]]:
    """The episode ranges a release title names; [] when it names none."""
    return [(int(m.group(1)), int(m.group(2))) for m in pack_range_matches(title)]


def pack_range_excludes(cand: dict, parsed: dict, episode: int | None, alt: list[int] | None = None) -> bool:
    """`cand` is a pack (`parsed["batch"]`) whose named ranges contain neither `episode` nor an `alt` number.
    A pack that names no range, or no episode given, is not excluded. The prefilter drops such a pack as `pack range`;
    the pack pass's gate (`cli.pack_pass_decision`) does not count it."""
    if not (parsed.get("batch") and episode):
        return False
    ranges = pack_ranges(cand.get("title", ""))
    wanted = {int(episode), *(int(a) for a in (alt or []))}
    return bool(ranges) and not any(lo <= n <= hi for lo, hi in ranges for n in wanted)


def prefilter_reason(cand: dict, parsed: dict, allow_batches: bool = True, episode: int | None = None,
                     alt: list[int] | None = None) -> str | None:
    """Returns why a release is dropped, or None to keep it. A multi-file torrent is NOT a batch by
    itself: single-episode releases ship fonts/subtitle sidecars (AVRG, 3 files); the providers pick the video.
    A pack is kept when allowed and seeded; its size is judged on the picked file, not the pack.
    Given the episode, a pack whose named ranges contain neither it nor an `alt` number is dropped."""
    if parsed.get("batch"):
        if not allow_batches:
            return "batch"
        if pack_range_excludes(cand, parsed, episode, alt):
            return "pack range"
        return None if cand.get("seeders") else "no seeders"
    if not cand.get("seeders"):
        return "no seeders"
    size = cand.get("total_size") or 0
    if size and not (150 * 2**20 <= size <= 5 * 2**30):
        return "size"
    return None


# A pin's preferred group. Kept here rather than imported from `pins` (which imports
# this module); `pins.GROUP_BONUS` is the same number and a test holds them equal.
PIN_GROUP_BONUS = 30

# The prefilter's reasons as the short codes the release picker groups rows by.
PREFILTER_CODES = {"batch": "batch", "pack range": "pack_range", "no seeders": "seeders", "size": "size"}


VERIFIED_LANG_MARGIN = 20
RULE_VERIFIED_LANG = "verified_lang_within_20"
RULE_SCORE = "score_then_seeders"


def final_order(c: dict) -> tuple:
    """The sort key of a ranked list: score, then seeders, then not-English-only."""
    return (c["score"], c["seeders"] or 0, not c.get("english_only"))


def order_ranked(ranked: list[dict], log: Log, lang: str, step: str) -> str | None:
    """Order `ranked` in place and return the rule that decided the top.

    A sort key cannot carry the decision rule — a verified-language candidate within 20 points of
    the winner goes first — because it compares each candidate with the top one. So every re-sort
    after `rank_candidates` (the TorBox cache bonus, the pack file pick) comes through here; a bare
    `sort` there undid the rule. The rule is judged on the scores as they are now.

    One `score order` line per call names the step, the rule and the winner; `changed` says whether
    the head differs from the one the list came in with. `score decision OVERRIDE` is logged only
    when the rule itself put a different release at the head, so one decision is one line. An empty
    list logs `result=EMPTY candidates=0` (every pack dropped by the file pick) and returns None."""
    if not ranked:
        log.info("score", "order", result="EMPTY", step=step, rule=None, winner=None, candidates=0,
                 reason="nothing left to order at this step")
        return None
    prev = ranked[0]["id"]
    ranked.sort(key=final_order, reverse=True)
    rule = RULE_SCORE
    if ranked[0].get("has_lang") is not True:
        for cand in ranked[1:]:
            if cand.get("has_lang") is True and ranked[0]["score"] - cand["score"] <= VERIFIED_LANG_MARGIN:
                over = ranked[0]
                ranked.remove(cand)
                ranked.insert(0, cand)
                rule = RULE_VERIFIED_LANG
                if cand["id"] != prev:
                    log.info("score", "decision", result="OVERRIDE", step=step, rule=rule, winner=cand["id"],
                             over=over["id"], winner_score=cand["score"], over_score=over["score"], prev_winner=prev,
                             reason=f"verified {lang} within {VERIFIED_LANG_MARGIN} points")
                break
    log.info("score", "order", result="PASS", step=step, rule=rule, winner=ranked[0]["id"],
             title=ranked[0].get("title"), score=ranked[0]["score"], has_lang=ranked[0].get("has_lang"),
             rd_cached=bool(ranked[0].get("rd_cached")), candidates=len(ranked),
             changed=ranked[0]["id"] != prev, prev_winner=prev)
    return rule


# A read in flight gets at least this long, whatever is left of `subtitleCheckSeconds`: above the slowest
# read measured (3.99 s; resolve-phase p99 1.95 s), so a healthy read is never cut, and the worst case is the budget
# plus this. A read cut at it or later is a hung add-on, not a slow one: it is skipped for the rest of the command, as
# a timeout at its own budget is. FAILED_READS_STOP failed reads in a row from one add-on: its site is failing, and each
# more costs a timeout, so its releases are passed over for the rest of the check; it ends when no other add-on can
# read what is left.
READ_FLOOR_S = 5.0
FAILED_READS_STOP = 2


def check_seconds(settings: dict) -> float:
    """`subtitleCheckSeconds`. Stored values are clamped to whole seconds (`core.SETTING_BOUNDS`); a fraction is read
    here so a test need not sleep, as `indexers.budget` does."""
    try:
        value = float(settings.get("subtitleCheckSeconds", DEFAULT_SETTINGS["subtitleCheckSeconds"]))
    except (TypeError, ValueError):
        return float(DEFAULT_SETTINGS["subtitleCheckSeconds"])
    return value if value > 0 else float(DEFAULT_SETTINGS["subtitleCheckSeconds"])


def rank_candidates(http: Http, log: Log, releases: list[dict], settings: dict,
                    dropped_sink: dict | None = None, episode: int | None = None,
                    alt: list[int] | None = None, rejected_sink: list | None = None,
                    keep_all: bool = False, preferred_group: str | None = None,
                    track_source: SubtitleTracks | None = None, season: int | None = None,
                    titles: Iterable[str] = ()) -> list[dict]:
    """Prefilter, pre-rank, verify subtitle tracks on the top N, score, order.

    `track_source` is the subtitle-track capability (`indexers.SubtitleTracks`): a release is
    verified only when the indexer that returned it offers it. Without it (None, or no such add-on)
    nothing is verified here and the score rests on title-parsed languages and learned group stats.

    `dropped_sink` collects the prefilter's reasons as `{reason: count}`. They were debug lines and
    nothing else, so the one fact that explains the whole Lantern x Lantern incident — 21 of 25
    releases dropped for "no seeders", i.e. every single-episode release of a 2011 show is dead —
    could not be reached by the code that has to tell the user what went wrong.

    `rejected_sink` collects `(release, code)` for each drop, so the release picker can
    list it; `keep_all` ranks every release whatever the prefilter says (the viewer picked it);
    `preferred_group` is a pin's group, worth `pins.GROUP_BONUS` (30) to its releases.

    `season` is the entry's season (`entry_season`) and `titles` its titles; a title is parsed with them,
    so a season pack of that season the search kept is a pack here too (`parse_title`'s `pack_by`, on the
    `score candidate` line).
    """
    lang = settings["subtitleLanguage"]
    now = time.time()
    group_stats = load_group_stats()
    static = static_group_priors(settings)
    english_only = english_only_groups(settings, lang)
    log.info("score.groups", "static", lang=lang, table=static, english_only=sorted(english_only),
             reason="preferredGroups is a static prior that learned stats beat; englishOnlyGroups only "
                    "breaks ties, and only when the language is not English")
    known_tracks = cached_json(CACHE_DIR / "tracks.json", None) or {}
    pre = []
    for r in releases:
        parsed = parse_title(r.get("title", ""), season, titles)
        cand = {
            "id": r.get("id"), "title": r.get("title"), "info_hash": r.get("info_hash"),
            # Add-ons spell it `magnet_uri` or `magnet`. Reading only the first dropped every
            # `magnet` here, with the trackers it carried, and the spawn fell back to a bare `xt`.
            "magnet": r.get("magnet_uri") or r.get("magnet"), "seeders": r.get("seeders"), "leechers": r.get("leechers"),
            "total_size": r.get("total_size"), "num_files": r.get("num_files"), "status": r.get("status"),
            "timestamp": r.get("timestamp"), "anidb_eid": r.get("anidb_eid"), "parsed": parsed,
            # Where `seeders` came from when it is not the index's own live count (a pin's).
            "seeders_source": r.get("seeders_source"),
            "source": r.get("source"), "trusted": r.get("trusted", False), "remake": r.get("remake", False),
            "verified_langs": known_tracks.get(str(r.get("info_hash") or "").lower()),
            # Which add-on returned it; with `id`, how its subtitle tracks are asked for.
            "indexer": r.get("indexer"),
            # The later indexers that returned the same infohash, `{indexer, release_id}` each.
            "also_from": r.get("also_from"),
        }
        reason = prefilter_reason(cand, parsed, allow_batches=as_bool(settings.get("allowBatches"), True),
                                  episode=episode, alt=alt)
        if reason and keep_all:
            log.info("score.prefilter", "override", id=cand["id"], reason=reason, title=cand["title"],
                     why="the viewer picked this release", **({"pack_by": parsed["pack_by"]} if parsed["pack_by"] else {}))
        elif reason:
            log.debug("score.prefilter", "drop", id=cand["id"], reason=reason, num_files=cand["num_files"],
                      seeders=cand["seeders"], bytes=cand["total_size"], title=cand["title"],
                      **({"pack_by": parsed["pack_by"]} if parsed["pack_by"] else {}))
            if dropped_sink is not None:
                dropped_sink[reason] = dropped_sink.get(reason, 0) + 1
            if rejected_sink is not None:
                rejected_sink.append((r, PREFILTER_CODES.get(reason, reason)))
            continue
        cand["pre_score"], _ = score_candidate(cand, parsed, lang, now, stage="pre",
                                               preferred_resolution=settings["preferredResolution"],
                                               group_stats=group_stats, static_priors=static)
        pre.append(cand)
    pre.sort(key=lambda c: (c["pre_score"], c["seeders"] or 0), reverse=True)
    log.info("score.prefilter", "done", result="PASS" if pre else "EMPTY", kept=len(pre), dropped=len(releases) - len(pre))

    # Lazy verification: a subtitle-track lookup costs wall time (one request to the add-on's site:
    # a median 0.92 s, p90 1.47 s), so verify in pre-rank order and stop at the first release confirmed to
    # carry the preferred language; the rest keep their title-based scores.
    # `subtitleCheckCount` caps the reads, the lookups that cost a request, not the candidates walked.
    # A release whose indexer offers no tracks (a Torznab one) or whose tracks are already known costs nothing, and
    # used to take a place: in one measured resolve 7 of the first 16 were such releases, so 9 were read and the one
    # carrying the viewer's language, 10th of the readable ones, never was.
    cap = as_int(settings.get("subtitleCheckCount"), DEFAULT_SETTINGS["subtitleCheckCount"])
    # And `subtitleCheckSeconds` caps their time. No read starts once it is spent, and a read in flight
    # is cut at what is left of it (at least READ_FLOOR_S), so a slow or blackholed site costs about the budget, not
    # `cap` reads of up to ~49 s each (`Http`'s 15 s x 3 attempts); FAILED_READS_STOP failed reads in a row from one
    # add-on pass over its releases, as its site is failing.
    budget = check_seconds(settings)
    walk = pre if cap > 0 else []
    if track_source is None or not track_source.offered:
        log.info("score.tracks", "skip", result="SKIP", candidates=len(walk),
                 reason="no loaded indexer add-on offers subtitle tracks; languages come from titles and "
                        "learned group stats")
        walk = []
    # Why the loop stopped (`found`, `cap`: `cap` reads spent with a readable candidate left, or `exhausted`: no
    # candidate left that could be read), and what it spent: `reads` is `failed` + `incomplete` + the complete
    # ones; `known` releases were passed over on their known tracks, `no_tracks` ones no add-on offers to read, `hung`
    # ones only an add-on that hung in this command could read, `failing` ones only a failing add-on could.
    def readable(c: dict, skip=frozenset()) -> bool:
        return c.get("verified_langs") is None and track_source.target(c, skip=skip) is not None

    stop, checked, reads = "exhausted", 0, 0
    failed_run: dict[str, int] = {}         # failed reads in a row, per add-on (`read_from.indexer`)
    failing: set[str] = set()               # add-ons with FAILED_READS_STOP of them: not asked again in this check
    count = {"failed": 0, "incomplete": 0, "known": 0, "no_tracks": 0, "hung": 0, "failing": 0}
    t0 = time.monotonic()
    for n, cand in enumerate(walk, 1):
        spent = time.monotonic() - t0
        limit = ("cap" if reads >= cap
                 else "failing" if failing and not any(readable(c, failing) for c in walk[n - 1:])
                 else "budget" if spent >= budget else None)
        if limit:
            if any(readable(c) for c in walk[n - 1:]):
                stop = limit
            break
        checked = n
        if cand.get("verified_langs") is not None:
            count["known"] += 1
            if has_language(cand["verified_langs"], lang):
                log.info("score.tracks", "early_stop", result="PASS", after=n, id=cand["id"], reason="known tracks")
                stop = "found"
                break
            continue
        # The add-on's own budget (`indexerTimeoutSeconds`) applies unless what is left is less; a read cut at either
        # marks the add-on hung, since the deadline is never under the floor.
        deadline = max(READ_FLOOR_S, budget - spent)
        rec = track_source.read(cand, None if deadline >= track_source.budget_s else deadline, skip=failing,
                                deadline_of="subtitleCheckSeconds", hangs=deadline >= READ_FLOOR_S)
        if rec is None:
            # its indexer offers no subtitle tracks; verified later via ffprobe on the stream
            count["no_tracks" if track_source.reg.tracks_ref(cand) is None
                  else "hung" if track_source.target(cand) is None else "failing"] += 1
            continue
        reads += 1      # a failed or incomplete read cost its request too
        if rec["status"] in ("failed", "incomplete"):
            count[rec["status"]] += 1
        who = rec["read_from"]["indexer"]
        failed_run[who] = failed_run.get(who, 0) + 1 if rec["status"] == "failed" else 0
        if failed_run[who] >= FAILED_READS_STOP:
            failing.add(who)
            log.warn("score.tracks", "failing", result="SKIP", indexer=who, failed=failed_run[who], after=n,
                     reason=f"{failed_run[who]} reads from {who} failed in a row; its releases are passed over for the "
                            f"rest of this check",
                     suggest="the indexers subtitle_tracks lines above say why each failed")
        cand["verified_langs"] = rec["languages"]
        if rec["first_read"] and rec["languages"] is not None:
            # Every record this loop reads is evidence about what the group ships, and every one of
            # them used to be thrown away: `record_group_langs` only ran after an ffprobe of a
            # streamed release or an mpv track readout, so the prior saw roughly one release per
            # play. `first_read` gates it so a release the indexer has read before is not counted twice.
            record_group_langs(parsed_of(cand), rec["languages"], lang)
        cand["tracks_status"] = rec["status"]
        cand["tracks_from"] = rec["read_from"]          # the track ids below are this indexer's
        # Per-track records (lang, format, forced, url) ride along for the side-load stage; nothing
        # in the formula reads them, so the score is unchanged.
        cand["tracks"] = rec["tracks"]
        if has_language(cand["verified_langs"], lang):
            log.info("score.tracks", "early_stop", result="PASS", after=n, id=cand["id"],
                     reason=f"verified {lang} on pre-rank #{n}")
            stop = "found"
            break
    if walk:
        unchecked = len(pre) - checked
        readable_left = sum(1 for c in pre[checked:] if readable(c))
        left = f"; {count['hung']} passed over because their add-on hung" if count["hung"] else ""
        hung = sorted(track_source.hung)
        if hung and not count["hung"]:
            left = f"; {', '.join(hung)} hung in this command and is not asked again"
        spent = time.monotonic() - t0
        unread = f"{readable_left} of {unchecked} candidates left could be read"
        (log.warn if stop == "failing" else log.info)(
                 "score.tracks", "stop", result={"found": "PASS", "cap": "CAPPED", "budget": "CAPPED",
                                                 "failing": "FAIL", "exhausted": "EMPTY"}[stop],
                 stop=stop, lang=lang, checked=checked, reads=reads, **count, cap=cap, candidates=len(pre),
                 unchecked=unchecked, unchecked_readable=readable_left, spent_s=round(spent, 2), budget_s=budget,
                 hung_addons=hung, failed_addons=sorted(failing),
                 reason={"found": f"a read release carries {lang}",
                         "cap": f"subtitleCheckCount ({cap}) track lists read without {lang}; {unread}",
                         "budget": f"subtitleCheckSeconds ({budget:g} s) spent on {reads} "
                                   f"read{'' if reads == 1 else 's'} without {lang}; {unread}",
                         "failing": f"the last {FAILED_READS_STOP} reads from {', '.join(sorted(failing))} failed, "
                                    f"and no other add-on can read what is left; {unread}",
                         "exhausted": f"no candidate left that could be read; none read carries {lang}{left}"}[stop],
                 suggest={"cap": "a release past the cap may carry the language unread; a higher subtitleCheckCount "
                                 "reads further, one request each",
                          "budget": "a release past the budget may carry the language unread; the reads were slow "
                                    "(see spent_s): a higher subtitleCheckSeconds reads further",
                          "failing": "the add-on's site is failing; the indexers subtitle_tracks lines above say why"
                          }.get(stop, ""))
    propagate_verification(pre, log)
    # With the language on no candidate, a read list without it scored -40 and an unread release 0, so
    # the pick slid to whichever release was not read. Decided once here, after every read of this resolve; the flag
    # rides on each candidate so the file-pick rescore (`cli.pick_pack_files`) scores it the same way.
    fallback_lang = fallback_language(settings)
    fallback = language_fallback(pre, lang, fallback_lang)
    log.info("score", "fallback", result="APPLIED" if fallback["applies"] else "SKIP", lang=lang,
             fallback_lang=fallback_lang or FALLBACK_OFF, **fallback,
             points={"subs.fallback_lang": FALLBACK_LANG_POINTS, "subs.stated_other": STATED_OTHER_POINTS})
    for cand in pre:
        cand["lang_fallback"] = {"second": fallback["second"]} if fallback["applies"] else None
    for cand in pre:
        parsed = cand.pop("parsed")
        total, terms = score_candidate(cand, parsed, lang, now, stage="final",
                                       preferred_resolution=settings["preferredResolution"],
                                       group_stats=group_stats, static_priors=static)
        if preferred_group and str(parsed.get("group") or "").lower() == str(preferred_group).lower():
            terms["pin.group"] = PIN_GROUP_BONUS
            total += PIN_GROUP_BONUS
        cand["score"], cand["terms"], cand["parsed"] = round(total, 1), terms, parsed
        cand["has_lang"] = has_language(cand["verified_langs"], lang)
        cand["english_only"] = str(parsed.get("group_key") or "") in english_only
        # When any mention of the language was read as a dub, the line says which part and whether the
        # bonus went or stayed, so a missed subtitle release can be told from a dub.
        dub = title_language_trace(cand["title"], lang)
        source = source_trace(cand["title"] or "", lang)
        log.info("score", "candidate", id=cand["id"], indexer=cand["indexer"], title=cand["title"],
                 score=cand["score"], terms=terms,
                 english_only=cand["english_only"], **({"pack_by": parsed["pack_by"]} if parsed["pack_by"] else {}),
                 verified_langs=sorted(set(cand["verified_langs"])) if cand["verified_langs"] else None, **dub,
                 **source,
                 inferred_from_sibling=bool(cand.get("inferred_from_sibling")), fallback=fallback["applies"],
                 **stated_trace(cand["title"] or "", lang, cand["lang_fallback"], cand["has_lang"], terms))
    order_ranked(pre, log, lang, "rank")      # logs `score order step=rank` with the winner and the rule
    return pre
