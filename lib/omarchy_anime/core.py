"""Shared constants, settings, tokens, structured logging, HTTP with retries, disk cache."""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import functools
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.5"
# The name the plugin's own directories and runtime files carry, so it shares none of them with another
# plugin with the same CLI name; the CLI binary keeps its `omarchy-anime` name because it lives
# inside the plugin folder.
APP_NAME = "omaanime"
USER_AGENT = f"{APP_NAME}/{VERSION} (+https://github.com/AsemHub/OmaAnime)"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / APP_NAME
# An empty XDG_RUNTIME_DIR is unset, as the launcher's `${XDG_RUNTIME_DIR:-…}` reads it (a relative socket otherwise).
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
MPV_SOCKET = RUNTIME_DIR / f"{APP_NAME}-mpv.sock"  # keep short: sun_path is 108 bytes
PLAYER_LAUNCHER = Path(__file__).resolve().parents[2] / "bin" / "omarchy-anime-player"

ANIZIP_URL = "https://api.ani.zip/mappings"
RD_URL = "https://api.real-debrid.com/rest/1.0"
TORBOX_URL = "https://api.torbox.app/v1/api"

DEFAULT_SETTINGS = {
    # English by default; a stored value (`ara` included) is kept as it is.
    "subtitleLanguage": "eng",
    # The second language when no release has the viewer's (`score.language_fallback`), or `off`.
    "subtitleFallbackLanguage": "eng",
    "subtitlePolicy": "prefer",      # prefer | require
    # A dubbed release (its name, or its probed audio without the show's original language, says so): `avoid` never
    # plays one while an original-audio release exists and waits for TorBox to download that one instead;
    # `allow` ranks and plays dubs as before.
    "dubs": "avoid",                 # avoid | allow
    "preferredResolution": 1080,
    # Indexer add-ons by file stem, asked in this order; files in the indexers
    # folder that are not listed follow by name, so a dropped-in file works without editing this.
    "indexers": [],
    "indexerTimeoutSeconds": 60,     # one indexer's whole episode search; a hung add-on is skipped after it
    # Releases whose subtitle tracks are read per play, best-ranked first (any add-on offering
    # subtitle tracks).
    "subtitleCheckCount": 16,
    # Seconds those reads may take per play. No read starts past it, and one in flight is cut at it
    # (`score.READ_FLOOR_S` at least). A read measured a median 0.92 s (p90 1.47 s): 16 of them about 15 s.
    "subtitleCheckSeconds": 15,
    # The ranking's static fansub-group prior, empty by default — the plugin
    # names no group. `preferredGroups` entries are `name` or `name=points`; `englishOnlyGroups` are
    # names only. A group's learned statistics beat both once it has any (`score.group_prior`).
    "preferredGroups": [],
    "englishOnlyGroups": [],
    # Stream providers in order. A provider is skipped when its token is missing; the plugin never
    # streams a torrent itself. Real-Debrid filters anime WEB releases, so TorBox leads.
    "providers": ["torbox", "realdebrid"],
    "probeCount": 3,                 # candidates probed per debrid provider; each may cost one ffprobe
    # How long a provider that refused the account stays skipped. Minutes, never a session
    # flag: a renewed plan has to recover on its own, without the user editing a setting.
    "providerBlockMinutes": 15,
    "rdPollSeconds": 30,             # cap on waiting for magnet conversion
    # When a play waited for TorBox to download its one uncached add: the longest wait, from the play's start until
    # TorBox has it, after which the episode still starts playing by itself. A longer wait loads it paused ("Space
    # plays it") with a notification and a sound, since the viewer will have looked away; nothing plays to an empty
    # room and AniList is not written behind their back. Whether the player is shown or hidden changes nothing.
    # 0: always paused. It does not end the wait: the play waits until TorBox is done, the viewer chooses, or
    # `providers.KEEP_WAITING_MAX_S`.
    "torboxWaitSeconds": 60,
    # TorBox's torrent info reads an uncached pack's file list from the BitTorrent network when
    # no loaded add-on lists files. Seconds TorBox may search; 0 turns the lookup off.
    "torrentInfoSeconds": 5,
    "httpTimeout": 15,
    # AniList API client "OmaAnime" (pin-flow, redirect https://anilist.co/api/v2/oauth/pin). Implicit-grant ids are public.
    "anilistClientId": 51175,
    # User-facing settings, editable from the bar widget's settings form.
    "watchedThreshold": 85,          # percent of the file after which the episode is marked watched
    "autoNext": False,               # start the next aired episode when one ends
    # Two minutes watched of an episode, download the next aired one at TorBox when it has not cached it (`prefetch`).
    "prefetchNext": True,
    "pollIntervalSec": 5,            # mpv position poll in the service
    "showTitleInBar": False,
    "maxTitleWidth": 180,
    "showCompletedInTop": False,
    "allowBatches": True,            # season packs are candidates; the episode's file is streamed
    # Side-loaded subtitles.
    "sideloadSubtitles": True,       # run the side-load stage at all (schema + defaults)
    "subtitleSyncMaxOffset": 60,     # anchor search window in seconds; 0 disables retiming (schema)
    "subtitleFont": "",                    # "" = automatic, follows subtitleLanguage (player.subtitle_font)
    "subsCacheMaxBytes": 16 * 1024 * 1024,  # config.json only; `subs prune` default
}


def as_bool(value, default: bool | None = False) -> bool | None:
    """A boolean from a settings value that may have arrived as a string.

    The shell's settings form writes real JSON booleans, but `omarchy bar set <id> <key> <value>`
    writes the value as a **string** unless `--json` is passed — and `bool("false")` is True, so a
    user who turned a feature off from the terminal would find it still on.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off", ""):
            return False
        return default
    return bool(value)


def as_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default

# What a file-list answer is, whoever gives it (an add-on's `file_list`, TorBox's torrent info).
# COMPLETE: the whole torrent (a list without the episode drops the pack); INCOMPLETE: part of it (can pick,
# never drops); UNKNOWN: not known (the pack stays); FAILED: the call failed (logged; read as unknown).
FILES_COMPLETE, FILES_INCOMPLETE, FILES_UNKNOWN, FILES_FAILED = "complete", "incomplete", "unknown", "failed"


# The longest release title and pack file name the plugin reads: every reader of a name is linear or close
# to it at this length, and a hostile name of megabytes cost minutes of CPU per title. Recorded maxima 259 (a title,
# the harness capture) and 207 (a pack file's path, the capture's TorBox answers): about 4x and 5x the margin.
RELEASE_TITLE_MAX = 1000
PACK_FILE_NAME_MAX = 1000
# The most digits a number field or a number in a name is read with: `int()` refuses more than 4,300 and a
# float overflows past 308. The largest recorded value is a pack's size of 1,429,365,116,108 bytes (13 digits).
NUMBER_DIGITS_MAX = 18


def cut_title(title: str) -> str:
    """A release title as the plugin reads it: its first `RELEASE_TITLE_MAX` characters."""
    return title[:RELEASE_TITLE_MAX]


def cut_file_name(name: str) -> str:
    """A pack file's name as the plugin reads it: its last `PACK_FILE_NAME_MAX` characters (the file's own name and
    extension are at the end)."""
    return name[-PACK_FILE_NAME_MAX:]


# The largest file size read, in bytes: `NUMBER_DIGITS_MAX` digits.
FILE_SIZE_MAX = 10 ** NUMBER_DIGITS_MAX


def file_size(value) -> int | float | None:
    """A file's size in bytes as a provider or an add-on gave it, or None when it is not a number above 0, not a finite
    one or of more than `NUMBER_DIGITS_MAX` digits (`int(inf)` of an infinite size ended a movie's play)."""
    ok = (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
          and 0 < value < FILE_SIZE_MAX)
    return value if ok else None


def size_refused(value) -> bool:
    """A size `file_size` refuses for being infinite or too large (not one of 0 or less: those never were sizes)."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
            and file_size(value) is None)


# A pack file as every file-list source is read (an add-on's `file_list`, TorBox's lists).
def pack_file(f: dict) -> dict | None:
    """One file of a release as the plugin reads it, whoever listed it: `name` (a path inside the torrent,
    or a bare name, its last `PACK_FILE_NAME_MAX` characters: the file's own name and extension are at the end) and
    `size` (`file_size`). None for an entry without a name."""
    name = str(f.get("name") or "")
    if not name:
        return None
    return {"name": cut_file_name(name), "size": file_size(f.get("size"))}


def pack_files(raw, log: "Log", component: str, **about) -> list[dict]:
    """`pack_file` of every dict in a file-list answer, with one `names_cut` line when a name was cut and one
    `sizes_refused` line when a size was refused."""
    raw = [x for x in raw or [] if isinstance(x, dict)]
    log_names_cut(log, component, [str(x.get("name") or "") for x in raw], **about)
    log_sizes_refused(log, component, [x.get("size") for x in raw], **about)
    return [f for f in (pack_file(x) for x in raw) if f]


def log_sizes_refused(log: "Log", component: str, sizes: list, **about) -> None:
    """One WARN line for a file list holding sizes `file_size` refuses: how many, one example, the limit."""
    refused = [s for s in sizes if size_refused(s)]
    if refused:
        log.warn(component, "sizes_refused", result="PARTIAL", **about, sizes_refused=len(refused),
                 example=repr(refused[0])[:40], limit=FILE_SIZE_MAX,
                 suggest="a file size of the list is infinite or past any real one; it is read as unknown")


def log_names_cut(log: "Log", component: str, names: list[str], **about) -> None:
    """One WARN line for a file list holding names over `PACK_FILE_NAME_MAX`: how many, the longest, the limit."""
    over = [len(n) for n in names if len(n) > PACK_FILE_NAME_MAX]
    if over:
        log.warn(component, "names_cut", result="PARTIAL", **about, cut=len(over), longest=max(over),
                 limit=PACK_FILE_NAME_MAX, suggest="a file name of the list is longer than any recorded one; only its "
                                                   "last characters are read")


def as_list(value, default: list) -> list:
    """A list of strings from a settings value that may have arrived as a string.

    **A JSON array cannot be stored in the shell.json widget entry.** Measured 2026-09-16: a
    top-level array value is coerced to its `Array.prototype.toString` — a comma join — somewhere
    in Omarchy's own write path. `omarchy-shell omaanime setSetting providers '["realdebrid"]'` and
    `omarchy bar set … '["realdebrid"]' --json` both land on disk as the string `"realdebrid"`,
    while an object, a number, and an array *nested inside an object* all survive intact. So the
    settings sheet writes a comma-joined string, this reads it back, and a hand-edited config.json
    keeps working with a real JSON array. Same shape as `as_bool`/`as_int`: never raises.
    """
    if isinstance(value, (list, tuple)):
        out = [str(v).strip() for v in value]
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return list(default)
        parsed = None
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
        if isinstance(parsed, list):
            out = [str(v).strip() for v in parsed]
        else:
            out = [part.strip() for part in text.split(",")]
    else:
        return list(default)
    out = [v for v in out if v]
    return out or list(default)


# Bounds for the integer settings, enforced in Python and not only in the control: a
# hand-edited config.json can carry anything and `omarchy bar set` takes any string. An
# out-of-range value falls back to the default with one note rather than failing a play.
SETTING_BOUNDS = {
    "preferredResolution": (240, 4320),
    "subtitleCheckCount": (1, 40),
    "subtitleCheckSeconds": (1, 120),
    "probeCount": (1, 10),
    # The ceiling is also the hard cap `providers.BlockGuard` refuses to honour past, so a
    # hand-edited cache file can never disable a provider for longer than this.
    "providerBlockMinutes": (1, 120),
    "rdPollSeconds": (5, 120),
    "torboxWaitSeconds": (0, 600),
    "torrentInfoSeconds": (0, 30),
    "httpTimeout": (5, 60),
    "indexerTimeoutSeconds": (5, 300),
    "subsCacheMaxBytes": (1024 * 1024, 512 * 1024 * 1024),
    "anilistClientId": (1, 10 ** 9),
    "watchedThreshold": (50, 100),
    "pollIntervalSec": (2, 30),
    "maxTitleWidth": (0, 560),
    "subtitleSyncMaxOffset": (0, 120),
}

KNOWN_PROVIDERS = ("torbox", "realdebrid")
# The settings that take one word of a fixed list. A value outside it (a typo through `omarchy bar set`) gets the
# default and a note, like an out-of-range integer; case and the spaces around it are folded silently.
CHOICE_SETTINGS = {"dubs": ("avoid", "allow")}
# Every key whose default is a boolean, so a new one is coerced without a list to keep.
BOOL_SETTINGS = tuple(k for k, v in DEFAULT_SETTINGS.items() if isinstance(v, bool))

# What a bare `preferredGroups` name is worth and the bounds on an explicit `=points`.
# The scale is the learned prior's (`score.group_prior`: -20 for a group that never shipped the
# language, +8 / -8 for one sample, +15 for a reliable one), so a bare name weighs what one good
# observation would, until real observations replace it. `englishOnlyGroups` adds no points: it
# only breaks ties of score and seeders (`score.english_only_groups`), so it never reorders what
# score and seeders rank (measured: a -8 penalty reordered 4 of 7 live resolves).
PREFERRED_GROUP_POINTS = 8
GROUP_POINTS_BOUNDS = (-20, 20)
GROUP_SETTINGS = {"preferredGroups": True, "englishOnlyGroups": False}   # key -> takes `=points`
# ASCII digits only, like the sheet's validator (`BrowseModel.isGroupEntry`): `\d` would otherwise
# take Arabic-Indic digits the sheet refuses.
_GROUP_ENTRY = re.compile(r"^([^,=\x00-\x1f]{1,64}?)\s*(?:=\s*(-?\d{1,4}))?$", re.ASCII)


def parse_group_entry(entry, weighted: bool = True) -> tuple[str, int] | None:
    """`"Name"` or `"Name = 12"` -> `("name", points)`; None for anything else.

    The name is compared with `score.parse_title`'s `group_key`, which is the lowercased tag, so it
    is lowercased here. Points are returned as written; `coerce_settings` clamps them. An entry
    with points where none are taken (`weighted=False`) is malformed, not silently re-read."""
    m = _GROUP_ENTRY.match(str(entry if entry is not None else "").strip())
    if not m or not m.group(1).strip():
        return None
    if m.group(2) is not None and not weighted:
        return None
    points = int(m.group(2)) if m.group(2) is not None else PREFERRED_GROUP_POINTS
    return m.group(1).strip().lower(), points


def coerce_settings(raw: dict) -> tuple[dict, list]:
    """Clamp and type the merged settings. Returns the settings and one note per correction.

    Pure, so the caller decides how loud to be: `load_settings_with_sources` stashes the notes and
    `cli.main` logs one `warn` line each, naming the key, what it was given and the bounds.
    """
    out = dict(raw)
    notes: list = []
    for key, (low, high) in SETTING_BOUNDS.items():
        if key not in out:
            continue
        given = out[key]
        value = as_int(given, -(10 ** 12))
        if value < low or value > high:
            out[key] = DEFAULT_SETTINGS[key]
            notes.append({"key": key, "given": given, "bounds": [low, high],
                          "using": DEFAULT_SETTINGS[key]})
        else:
            out[key] = value
    # `omarchy bar set` without `--json` stores "true"/"false", and the QML reads `=== true`, so a
    # string reached `status` as a string and the sheet showed the key off. A value `as_bool` reads is typed here
    # silently; anything else gets the default and a note, like an out-of-range integer. "Anything else"
    # includes a list, an object or a number other than 0 and 1, which `as_bool` alone reads by truthiness.
    for key in BOOL_SETTINGS:
        if key not in out or isinstance(out[key], bool):
            continue
        given = out[key]
        readable = isinstance(given, str) or (isinstance(given, int) and given in (0, 1))
        value = as_bool(given, None) if readable else None
        if value is None:
            out[key] = DEFAULT_SETTINGS[key]
            notes.append({"key": key, "given": given, "bounds": [True, False], "using": DEFAULT_SETTINGS[key],
                          "suggest": f"{key}: {given!r} is not true or false. Using {DEFAULT_SETTINGS[key]!r}"})
        else:
            out[key] = value
    for key in ("providers",):
        if key not in out:
            continue
        given = out[key]
        value = as_list(given, [])
        if key == "providers":
            # An empty provider list is a plugin that cannot play anything, so it is a bad-but-legal
            # value the Python side refuses rather than a state a user can reach from the sheet.
            value = [p for p in value if p in KNOWN_PROVIDERS]
        if not value:
            notes.append({"key": key, "given": given,
                          "bounds": list(KNOWN_PROVIDERS) if key == "providers" else ["1 or more"],
                          "using": list(DEFAULT_SETTINGS[key])})
            value = list(DEFAULT_SETTINGS[key])
        elif value != as_list(given, []):
            notes.append({"key": key, "given": given, "bounds": list(KNOWN_PROVIDERS),
                          "using": list(value)})
        out[key] = value
    for key, weighted in GROUP_SETTINGS.items():
        if key not in out:
            continue
        # Case and the spaces around a name or `=` are normalised silently; a dropped, clamped or
        # repeated entry gets a note, because that is the viewer's list not meaning what they typed.
        # A repeated group keeps its first entry, as `score.static_group_priors` reads it.
        given, value, corrected, seen = out[key], [], False, set()
        low, high = GROUP_POINTS_BOUNDS
        for entry in as_list(given, []):
            parsed = parse_group_entry(entry, weighted)
            if parsed is None:
                corrected = corrected or bool(entry)
                continue
            name, points = parsed
            if name in seen:
                corrected = True
                continue
            seen.add(name)
            if "=" not in entry:
                value.append(name)
                continue
            clamped = min(max(points, low), high)
            corrected = corrected or clamped != points
            value.append(f"{name}={clamped}")
        if corrected:
            notes.append({"key": key, "given": given,
                          "bounds": ["name=points", list(GROUP_POINTS_BOUNDS)] if weighted else ["name"],
                          "using": list(value)})
        out[key] = value
    for key in ("subtitleLanguage", "subtitleFallbackLanguage"):
        if key not in out:
            continue
        # A code the table does not hold has no aliases, title words, provider codes or codepages, so it is
        # refused with a note, not kept and silently degraded. An alias is folded to its code (`zho` -> `chi`).
        # A variant (`pt-BR`, `pt_br`) keeps the table code here, which every reader of the key uses, and goes to
        # `<key>Variant`; another region of a known language (`ar-SA`) is its language, with a note.
        given = out[key]
        off = key == "subtitleFallbackLanguage" and str(given).strip().lower() == FALLBACK_OFF
        code = FALLBACK_OFF if off else language_code(given) or fold_language(given)
        variant = None if off or code is None else language_variant(given)
        if code is None:
            bounds = list(LANGUAGE_TABLE) + list(LANGUAGE_VARIANTS) + \
                ([FALLBACK_OFF] if key == "subtitleFallbackLanguage" else [])
            notes.append({"key": key, "given": given, "bounds": bounds, "using": DEFAULT_SETTINGS[key],
                          "suggest": f"{key}: {given!r} is not a language the plugin knows; one of "
                                     f"{', '.join(bounds)}. Using {DEFAULT_SETTINGS[key]!r}"})
            code = DEFAULT_SETTINGS[key]
        elif not off and variant is None and language_code(given) is None:
            notes.append({"key": key, "given": given, "bounds": list(LANGUAGE_VARIANTS), "using": code,
                          "region": True,
                          "suggest": f"{key}: {given!r} names a region the plugin does not offer; using its "
                                     f"language {code!r} (regional choices: {', '.join(LANGUAGE_VARIANTS)})"})
        out[key] = code
        out.pop(f"{key}Variant", None)          # only coerce writes it: a stored one is not a choice
        if variant:
            out[f"{key}Variant"] = variant
    for key, choices in CHOICE_SETTINGS.items():
        if key not in out:
            continue
        given = out[key]
        word = str(given).strip().lower() if isinstance(given, str) else None
        if word in choices:
            out[key] = word
            continue
        notes.append({"key": key, "given": given, "bounds": list(choices), "using": DEFAULT_SETTINGS[key],
                      "suggest": f"{key}: {given!r} is not one of {', '.join(choices)}. "
                                 f"Using {DEFAULT_SETTINGS[key]!r}"})
        out[key] = DEFAULT_SETTINGS[key]
    if "indexers" in out:
        # An empty list is the normal state (every file in the folder, by name), so no note for it.
        out["indexers"] = [str(v).strip() for v in as_list(out["indexers"], []) if str(v).strip()]
    return out, notes


# The notes from the last `load_settings_with_sources()` call, so `cli.main` can log them once it
# has a Log to log them to. A module global rather than a return-value change because every caller
# of `load_settings()` would otherwise have to grow a parameter it does not want.
LAST_SETTING_NOTES: list = []
# Each key of the last `load_settings_with_sources()`, as `(where it came from, its value)`, for the
# resolve's settings line; and the values it refused, by key.
LAST_SETTING_SOURCES: dict = {}
LAST_SETTING_REFUSED: dict = {}


def setting_refused(key: str, settings: dict):
    """The stored value the last load refused for `key` (the default was used instead), or None; None too when the
    caller has since changed the value."""
    if key in LAST_SETTING_REFUSED and settings.get(key, DEFAULT_SETTINGS.get(key)) == DEFAULT_SETTINGS.get(key):
        return LAST_SETTING_REFUSED[key]
    return None


def setting_source(key: str, settings: dict) -> str:
    """`default`, `config` or `shell` for a key whose value is the one the last load gave it; for a value a caller
    set itself (a test, a replay, the picker's `prefer`), `default` when it is the default and `caller` otherwise."""
    given = settings.get(key, DEFAULT_SETTINGS.get(key))
    where, value = LAST_SETTING_SOURCES.get(key, (None, None))
    if where is not None and value == given:
        return where
    return "default" if given == DEFAULT_SETTINGS.get(key) else "caller"

PLUGIN_ID = "io.github.asemhub.omaanime"
SHELL_JSON = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "omarchy" / "shell.json"

# ISO 639-2 code -> (aliases accepted in track tags, words/tokens to look for in titles)
# Every language that is on a release's track list in a capture of 906 read
# releases and that OpenSubtitles or Subdl offers. Keyed by the ISO 639-2/B code the track lists use; the /T code,
# the two-letter code and the other tags the lists or mpv write are aliases. A code of three letters is read in a
# title only as a tag (`[THA]`), a longer word as a word (`score.language_title_regex`).
LANGUAGE_TABLE = {
    "ara": (("ara", "arb", "ar"), ("ara", "arabic")),
    "eng": (("eng", "en"), ("eng", "english")),
    "fre": (("fre", "fra", "fr"), ("fre", "french")),
    "ger": (("ger", "deu", "de"), ("ger", "german")),
    "spa": (("spa", "es"), ("spa", "spanish")),
    "por": (("por", "pt"), ("por", "portuguese")),
    "ita": (("ita", "it"), ("ita", "italian")),
    "rus": (("rus", "ru"), ("rus", "russian")),
    "tur": (("tur", "tr"), ("tur", "turkish")),
    "per": (("per", "fas", "fa"), ("per", "persian", "farsi")),
    "pol": (("pol", "pl"), ("pol", "polish")),
    "chi": (("chi", "zho", "zh", "chs", "cht", "zh-hans", "zh-hant", "zh-cn", "zh-tw", "zh-hk"), ("chi", "chinese")),
    "ind": (("ind", "id"), ("ind", "indonesian")),
    "tha": (("tha", "th"), ("tha", "thai")),
    "may": (("may", "msa", "ms", "zsm"), ("may", "malay", "melayu")),
    "vie": (("vie", "vi"), ("vie", "vietnamese")),
    "jpn": (("jpn", "ja"), ("jpn",)),
    "kor": (("kor", "ko"), ("kor", "korean")),
    "dan": (("dan", "da"), ("dan", "danish")),
    "hun": (("hun", "hu"), ("hun", "hungarian")),
    "fin": (("fin", "fi"), ("fin", "finnish")),
    "dut": (("dut", "nld", "nl"), ("dut", "dutch")),
    "swe": (("swe", "sv"), ("swe", "swedish")),
    "heb": (("heb", "he", "iw"), ("heb", "hebrew")),
    "gre": (("gre", "ell", "el"), ("gre", "greek")),
    "cze": (("cze", "ces", "cs"), ("cze", "czech")),
    "rum": (("rum", "ron", "ro"), ("rum", "romanian")),
    "nor": (("nor", "nob", "nno", "no", "nb", "nn"), ("nor", "norwegian")),
    "hrv": (("hrv", "hr"), ("hrv", "croatian")),
    "fil": (("fil", "tgl", "tl"), ("fil", "filipino", "tagalog")),
    "ukr": (("ukr", "uk"), ("ukr", "ukrainian")),
    "hin": (("hin", "hi"), ("hin", "hindi")),
    "cat": (("cat", "ca"), ("cat", "catalan")),
    "glg": (("glg", "gl"), ("glg", "galician")),
    "sin": (("sin", "si"), ("sin", "sinhala", "sinhalese")),
    "baq": (("baq", "eus", "eu"), ("baq", "basque")),
    "bos": (("bos", "bs"), ("bos", "bosnian")),
    "slo": (("slo", "slk", "sk"), ("slo", "slovak")),
}

# What each language is called, in English (the panel, the settings sheet, the side-loaded track's title)
# and in its own words (an mpv track whose tag is missing but whose title names it: `track_title_names`).
LANGUAGE_NAMES = {
    "ara": ("Arabic", ("العربية", "عرب")), "eng": ("English", ()), "fre": ("French", ("français", "francais")),
    "ger": ("German", ("deutsch",)), "spa": ("Spanish", ("español", "espanol", "castellano")),
    "por": ("Portuguese", ("português", "portugues")), "ita": ("Italian", ("italiano",)),
    "rus": ("Russian", ("русский",)), "tur": ("Turkish", ("türkçe", "turkce")), "per": ("Persian", ("فارسی",)),
    "pol": ("Polish", ("polski",)), "chi": ("Chinese", ("中文", "简体", "繁體", "繁体", "简中", "繁中")),
    "ind": ("Indonesian", ("bahasa indonesia",)), "tha": ("Thai", ("ไทย",)),
    "may": ("Malay", ("bahasa melayu",)), "vie": ("Vietnamese", ("tiếng việt", "tieng viet")),
    "jpn": ("Japanese", ("日本語",)), "kor": ("Korean", ("한국어",)), "dan": ("Danish", ("dansk",)),
    "hun": ("Hungarian", ("magyar",)), "fin": ("Finnish", ("suomi",)), "dut": ("Dutch", ("nederlands",)),
    "swe": ("Swedish", ("svenska",)), "heb": ("Hebrew", ("עברית",)), "gre": ("Greek", ("ελληνικά",)),
    "cze": ("Czech", ("čeština", "cestina")), "rum": ("Romanian", ("română", "romana")),
    "nor": ("Norwegian", ("norsk",)), "hrv": ("Croatian", ("hrvatski",)), "fil": ("Filipino", ()),
    "ukr": ("Ukrainian", ("українська",)), "hin": ("Hindi", ("हिन्दी", "हिंदी")), "cat": ("Catalan", ("català", "catala")),
    "glg": ("Galician", ("galego",)), "sin": ("Sinhala", ("සිංහල",)), "baq": ("Basque", ("euskara",)),
    "bos": ("Bosnian", ("bosanski",)), "slo": ("Slovak", ("slovenčina", "slovencina")),
}
# The `subtitleFallbackLanguage` value that turns the second language off: every release is scored by its name only.
FALLBACK_OFF = "off"
_LANGUAGE_KEYS = {alias.lower(): code for code, (aliases, _) in LANGUAGE_TABLE.items() for alias in (code, *aliases)}


def language_code(value) -> str | None:
    """The `LANGUAGE_TABLE` key a language setting names (its code, or one of its aliases in any case: `zho`, `FR`);
    None for anything else."""
    return _LANGUAGE_KEYS.get(str(value if value is not None else "").strip().lower())


# The region and script variants both language settings offer: each one's table language, its name, and
# the other tags that are the same variant. Only the in-file track pick, mpv's `slang` and the subtitle databases read
# a variant; everything else reads the table language (the index and ffprobe carry no region). Not aliases in
# `LANGUAGE_TABLE`: those build `score`'s title regexes, and a new one moves release picks.
LANGUAGE_VARIANTS = {
    "es-419": ("spa", "Spanish (Latin America)",
               ("es-AR", "es-BO", "es-CL", "es-CO", "es-CR", "es-CU", "es-DO", "es-EC", "es-GT", "es-HN", "es-MX",
                "es-NI", "es-PA", "es-PE", "es-PR", "es-PY", "es-SV", "es-UY", "es-VE")),
    "es-ES": ("spa", "Spanish (Spain)", ()),
    "pt-BR": ("por", "Portuguese (Brazil)", ()),
    "pt-PT": ("por", "Portuguese (Portugal)", ()),
    "zh-Hans": ("chi", "Chinese (Simplified)", ("zh-CN", "zh-SG", "chs")),
    "zh-Hant": ("chi", "Chinese (Traditional)", ("zh-TW", "zh-HK", "zh-MO", "cht")),
}
_VARIANT_KEYS = {tag.lower(): variant for variant, (_, _, same) in LANGUAGE_VARIANTS.items()
                 for tag in (variant, *same)}


def _tag(value) -> str:
    return str(value if value is not None else "").strip().lower().replace("_", "-")


def fold_language(tag) -> str | None:
    """The `LANGUAGE_TABLE` code of any language tag: a setting, an index code or an mpv track tag with a region or
    script (`ar-SA`, `pt_BR`, `zh-Hant`, `ara`); the whole tag first (`chs`), then its first subtag. None for a
    language the table does not hold."""
    t = _tag(tag)
    return _LANGUAGE_KEYS.get(t) or _LANGUAGE_KEYS.get(t.split("-")[0])


def same_language(tag, lang) -> bool:
    """`tag` (any region or script of it) is the language `lang` names; a `lang` the table does not hold matches its
    own first subtag."""
    code = fold_language(lang)
    if code is None:
        return bool(_tag(lang)) and _tag(tag).split("-")[0] == _tag(lang).split("-")[0]
    return fold_language(tag) == code


def language_variant(tag) -> str | None:
    """The `LANGUAGE_VARIANTS` key a tag is (`pt-br`, `es-MX` -> `es-419`, `zh-Hant-TW` and `cht` -> `zh-Hant`), or
    None (a bare language, or a region no setting offers: `ar-SA`)."""
    t = _tag(tag)
    parts = t.split("-")
    return _VARIANT_KEYS.get(t) or (_VARIANT_KEYS.get("-".join(parts[:2])) if len(parts) > 2 else None)


# The pick's steps for a track tag of the viewer's language (`tag_step`, `cli.pick_sub_track`), in its order.
TRACK_STEPS = ("exact", "base", "other_variant")


def tag_step(tag, lang, variant=None) -> str | None:
    """Which step of the track pick a track's tag answers for the table language `lang` and the chosen `variant`:
    `exact` (the variant or an equivalent), `base` (the bare language: `pt`, `por`, `arb`), `other_variant` (another
    region or script of it: `ar-SA`, `pt-PT`, `chs`), or None (another language, no tag)."""
    if not _tag(tag) or not same_language(tag, lang):
        return None
    found = language_variant(tag)
    if variant and found == variant:
        return "exact"
    return "base" if found is None and "-" not in _tag(tag) else "other_variant"


def setting_variant(settings: dict, key: str = "subtitleLanguage") -> str | None:
    """The variant the viewer chose for a language setting (`pt-BR`), or None: `coerce_settings`' `<key>Variant`, or
    the value itself when a caller set one; only while it is a variant of the setting's language."""
    value = settings.get(key)
    variant = settings.get(f"{key}Variant") or language_variant(value)
    if variant not in LANGUAGE_VARIANTS or LANGUAGE_VARIANTS[variant][0] != fold_language(value):
        return None
    return variant


def language_name(lang) -> str:
    """"ara" -> "Arabic", a variant by its own name (`pt-BR` -> "Portuguese (Brazil)", `zh-CN` -> "Chinese
    (Simplified)"), any other region by its language (`ar-SA` -> "Arabic"); a code the table does not hold,
    upper-cased."""
    variant = language_variant(lang)
    if variant:
        return LANGUAGE_VARIANTS[variant][1]
    code = fold_language(lang)
    return LANGUAGE_NAMES[code][0] if code else str(lang or "").upper()


def fallback_language(settings: dict) -> str | None:
    """The second subtitle language `score.language_fallback` uses, or None when it is `off`."""
    value = settings.get("subtitleFallbackLanguage", DEFAULT_SETTINGS["subtitleFallbackLanguage"])
    return None if str(value).strip().lower() == FALLBACK_OFF else (language_code(value) or str(value))


_TITLE_TOKEN = re.compile(r"[^\W_]+")


def track_title_names(title, lang) -> bool:
    """An mpv track's title names `lang`: one of its title words longer than three letters, its English name
    or its own names anywhere in the title (`Arabic`, `Japanese`, `Español (España)`, `ไทย`), or a three-letter word as
    a word of its own (`[ara]`, not the `vie` in `Movie`) or glued to `sub`/`subs` (`ENGSUB`, `EngSubs`)."""
    text = str(title or "").lower()
    code = language_code(lang) or str(lang or "").lower()
    words = LANGUAGE_TABLE.get(code, ((), (code,)))[1]
    english, names = LANGUAGE_NAMES.get(code, ("", ()))
    if any(w in text for w in words if len(w) > 3) or any(n in text for n in names) or (english and english.lower() in text):
        return True
    short = [w for w in words if len(w) <= 3]
    tokens = set(_TITLE_TOKEN.findall(text))
    return any(w in tokens or f"{w}sub" in tokens or f"{w}subs" in tokens for w in short)


# ----------------------------------------------------------------------------- logging

LOG_RETENTION_DAYS = 14
LOG_PRUNE_STAMP = ".pruned"


def set_private(path: Path, mode: int) -> int | OSError | None:
    """chmod to `mode` when the current one differs. Best effort: a log that cannot be tightened
    is still a log that has to be written. Returns the mode it had when it changed it, the error when it could not,
    None when it was already `mode`."""
    try:
        was = path.stat().st_mode & 0o777
        if was != mode:
            path.chmod(mode)
            return was
    except OSError as exc:
        return exc
    return None


# The cache folders this process has already made or tightened (`private_dir`).
_PRIVATE_DIRS: set[str] = set()
# Each folder `private_dir` changed or could not, waiting for the next log line to say so (`Log.emit` drains it: the
# log's own folder is made by `private_dir` before any `Log` exists).
_PRIVATE_NOTES: list[dict] = []


def private_dir(folder: Path) -> Path:
    """Make `folder`, and every missing folder between `CACHE_DIR` and it, at 0o700, and tighten one an older build
    made at the umask's mode (755). `mkdir(parents=True, mode=...)` gives the mode to the last folder only. The chmod
    runs once per folder per process; the `mkdir` every time, so a folder pruned meanwhile comes back. A folder
    outside `CACHE_DIR` (`$XDG_RUNTIME_DIR`, a test's) is made at 0o700 when missing and never chmod-ed: it is not
    the plugin's to change."""
    folder = Path(folder)
    try:
        parts = folder.relative_to(CACHE_DIR).parts
    except ValueError:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        return folder
    for p in (CACHE_DIR.joinpath(*parts[:i]) for i in range(len(parts) + 1)):
        p.mkdir(mode=0o700, parents=True, exist_ok=True)
        if str(p) not in _PRIVATE_DIRS:
            got = set_private(p, 0o700)
            _PRIVATE_DIRS.add(str(p))
            if got is not None:
                note = {"path": home_shown(str(p)), "to": "700"}
                note.update({"error": f"{type(got).__name__}: {got.strerror or got}"} if isinstance(got, OSError)
                            else {"from": f"{got:o}"})
                _PRIVATE_NOTES.append(note)
    return folder


def private_temp(path: Path) -> tuple[int, Path]:
    """A new temp file beside `path` for an atomic write: `<name>.tmp.<random>`, made by `tempfile.mkstemp` (mode
    0o600 from the first byte, `O_EXCL`, `O_NOFOLLOW`), so a planted file or symlink at a guessed name is never
    written through. Its folder is made private first (`private_dir`)."""
    import tempfile
    private_dir(path.parent)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.tmp.")
    return fd, Path(name)


def prune_logs(directory: Path, max_age_days: int = LOG_RETENTION_DAYS) -> int:
    """Delete JSONL logs older than `max_age_days`. Only *.jsonl: anything else in the directory
    is not ours to delete."""
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    try:
        candidates = list(directory.glob("*.jsonl"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def maybe_prune_logs(directory: Path) -> int | None:
    """Prune at most once a day, gated by a stamp file. Returns None when it was skipped.

    One day of development left a 9.7 MB JSONL here and nothing ever removed it; the trace viewer
    only ever reads the last few days by name, so anything older is pure disk.
    """
    stamp = directory / LOG_PRUNE_STAMP
    try:
        if time.time() - stamp.stat().st_mtime < 86400:
            return None
    except OSError:
        pass
    try:
        touch_private(stamp)
    except OSError:
        return None
    return prune_logs(directory)


# One lock for every `Log` in the process. Lines are emitted from threads (an add-on's guarded
# call, TorBox torrent-info lookups side by side); each line must reach stderr and the file whole.
_EMIT_LOCK = threading.Lock()


class Log:
    """JSONL trace log. One line per stage: ts, level, trace, component, stage, result, ms, data."""

    def __init__(self, trace: str):
        self.trace = trace
        log_dir = private_dir(CACHE_DIR / "log")
        maybe_prune_logs(log_dir)
        self.path = log_dir / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
        # The log holds URLs, media titles and every IPC reply. It is nobody else's
        # business, and `mkdir(exist_ok=True)` does not fix a directory created at the umask
        # default by an older build — so tighten both, once per process, before the first write.
        set_private(log_dir, 0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        set_private(self.path, 0o600)

    # `emit`'s own parameter names. A caller splatting a payload that happens to use one
    # of these — the UI's play.failed carries its own `stage` — hits "got multiple values for
    # argument" and the line is never written. Anything building an emit call from untrusted or
    # caller-supplied keys must route them around this set.
    RESERVED = ("level", "component", "stage", "result", "ms", "suggest")

    def emit(self, level: str, component: str, stage: str, result: str = "", ms: float | None = None,
             suggest: str = "", **data):
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": level, "trace": self.trace, "component": component, "stage": stage,
        }
        if result:
            line["result"] = result
        if ms is not None:
            line["ms"] = round(ms, 1)
        if suggest:
            line["suggest"] = suggest
        if data:
            line["data"] = data
        if _PRIVATE_NOTES and component != "cache":
            self._private_notes()
        # Scrub at the sink, so no future field can reopen the hole by forgetting to.
        text = json_controls(redact_text(json.dumps(line, ensure_ascii=False, default=str)))
        with _EMIT_LOCK:
            sys.stderr.write(text + "\n")
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(text + "\n")

    def _private_notes(self) -> None:
        """One `cache private` line per folder `private_dir` tightened (from, to) or could not (the error, and what
        to run): docs/privacy.md promises a folder an earlier build made wider is tightened on the next write."""
        with _EMIT_LOCK:
            notes = _PRIVATE_NOTES[:]
            del _PRIVATE_NOTES[:]
        for note in notes:
            if "error" in note:
                self.emit("warn", "cache", "private", result="FAIL", **note,
                          suggest=f"this folder stays readable by others; run: chmod 700 {note['path']}")
            else:
                self.emit("info", "cache", "private", result="PASS", **note)

    def info(self, component, stage, **kw):
        self.emit("info", component, stage, **kw)

    def debug(self, component, stage, **kw):
        self.emit("debug", component, stage, **kw)

    def warn(self, component, stage, **kw):
        self.emit("warn", component, stage, **kw)

    def error(self, component, stage, **kw):
        self.emit("error", component, stage, **kw)


class StageError(Exception):
    def __init__(self, stage: str, message: str, suggest: str = "", **data):
        super().__init__(message)
        self.stage, self.suggest, self.data = stage, suggest, data


class Interrupted(StageError):
    """The user stopped a waiting command (Ctrl-C). `main` reports it like any stage failure, one
    JSON result and no traceback, and exits 130, the shell's code for SIGINT."""


class Deferred(Interrupted):
    """The viewer asked to be told when TorBox's download is ready ("Tell me when it's ready") while the play waited
    for it: raised by the wait's handler of `playstate.DEFER_SIGNAL`, so `TorBox.wait_ready` lets it through as it lets
    a stop through. `do_play` ends the play `later`, not `failed`."""


class Held(StageError):
    """A play that waited for TorBox's download of its one uncached add (the original-audio release with only a dub
    cached, or the best release with nothing cached) reached the wait's cap (`providers.KEEP_WAITING_MAX_S`) without
    it and holds the viewer's choices (`data["choice"]`). Not a failure: `do_play` ends the play
    `held`, and no surface reads it as an error."""


class BlockedError(StageError):
    """A provider refuses this release on policy grounds (e.g. Real-Debrid 451). Skip, don't count as a probe."""


class ProviderUnavailableError(StageError):
    """The provider cannot take the request right now, for a reason on its own side (TorBox
    `NO_SERVERS_AVAILABLE_ERROR`, `DATABASE_ERROR`, `DOWNLOAD_SERVER_ERROR`, `AUTH_ERROR`). Not the
    release's fault and not the account's: it is never counted against the release, and never fed to the block
    guard, which is about the account."""


# ----------------------------------------------------------------------------- config

def shell_widget_settings(path: Path | None = None) -> dict:
    """The plugin's bar-widget entry in Omarchy's shell.json is the source of truth for user settings:
    `omarchy bar set io.github.asemhub.omaanime watchedThreshold 90` reaches the CLI and the QML side alike."""
    # Resolved at call time, not bound as a default: the tests point SHELL_JSON at a temp file.
    try:
        data = json.loads((path or SHELL_JSON).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    layout = ((data.get("bar") or {}).get("layout") or {}) if isinstance(data, dict) else {}
    for section in ("left", "center", "right"):
        for entry in layout.get(section) or []:
            if isinstance(entry, dict) and entry.get("id") == PLUGIN_ID:
                return {k: v for k, v in entry.items() if k != "id"}
    return {}


def plugin_enabled(path: Path | None = None) -> bool | None:
    """Whether Omarchy has this plugin enabled, read exactly as its shell reads it for an installed, third-party,
    non-bar plugin: `PluginRegistry.isEnabled` (Omarchy 4.0.4, `/usr/share/omarchy/shell/services/PluginRegistry.qml`
    lines 148-164, read 2026-10-20). Not when the id is a string of `disabledPlugins[]` (`isDisabled`, 166-169: an
    `indexOf` of the string, so an object there is not it); else `findEntryLocation` (231-248): `bar.id` is the id, or
    an entry of `bar.layout.left|center|right` is (`findBarLocation` 204-219, `barEntryId` 200: an object's `id`, or
    the entry itself), or an object of `plugins[]` has it as its `id`. None when shell.json cannot be read: unknown,
    never "disabled"."""
    try:
        data = json.loads((path or SHELL_JSON).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    disabled = data.get("disabledPlugins")
    if isinstance(disabled, list) and PLUGIN_ID in [d for d in disabled if isinstance(d, str)]:
        return False
    bar = data.get("bar") if isinstance(data.get("bar"), dict) else None
    if bar is not None and _js_string(bar.get("id")) == PLUGIN_ID:
        return True
    layout = bar.get("layout") if bar is not None and isinstance(bar.get("layout"), dict) else {}
    for section in ("left", "center", "right"):
        entries = layout.get(section)
        if isinstance(entries, list) and any(
                _js_string(e.get("id") if isinstance(e, dict) else e) == PLUGIN_ID for e in entries):
            return True
    plugins = data.get("plugins")
    return isinstance(plugins, list) and any(
        isinstance(e, dict) and _js_string(e.get("id")) == PLUGIN_ID for e in plugins)


def _js_string(value) -> str:
    """The id `Util.canonicalWidgetId` (`String(id || "")`) makes of a JSON value, as far as matching this plugin's id
    goes: a string is itself; any other value's string (a number's digits, "[object Object]", "true") is never it."""
    return value if isinstance(value, str) else ""


def load_settings_with_sources() -> tuple[dict, dict]:
    """The merged settings **and** where each key's effective value came from.

    Precedence: `default` < `config` (~/.config/omaanime/config.json, advanced
    knobs) < `shell` (the plugin's entry in Omarchy's shell.json, what the settings sheet and
    `omarchy bar set` write). The sheet shows the source under any control it is shadowing, because
    a value set in config.json that a later shell.json write silently overrides is exactly the kind
    of precedence a user cannot see and cannot debug.
    """
    settings = dict(DEFAULT_SETTINGS)
    sources = {k: "default" for k in settings}
    path = CONFIG_DIR / "config.json"
    if path.exists():
        try:
            layer = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StageError("config.load", f"{path} is not valid JSON: {exc}", "fix or delete the file")
        if isinstance(layer, dict):
            settings.update(layer)
            sources.update({k: "config" for k in layer})
    widget = shell_widget_settings()
    settings.update(widget)
    sources.update({k: "shell" for k in widget})
    settings, notes = coerce_settings(settings)
    LAST_SETTING_NOTES[:] = notes
    # A stored value that was refused and replaced by the default is not that store's: its
    # key is `default`, and `setting_refused` keeps what was given for the settings line.
    LAST_SETTING_REFUSED.clear()
    for note in notes:
        if note["using"] == DEFAULT_SETTINGS.get(note["key"]) and not note.get("region"):
            sources[note["key"]] = "default"
            LAST_SETTING_REFUSED[note["key"]] = note["given"]
    LAST_SETTING_SOURCES.clear()
    LAST_SETTING_SOURCES.update({k: (where, settings.get(k)) for k, where in sources.items()})
    return settings, sources


def load_settings() -> dict:
    """Defaults < ~/.config/omaanime/config.json (advanced knobs) < shell.json widget entry (user settings)."""
    return load_settings_with_sources()[0]


def is_private_mode(mode: int) -> bool:
    """The one rule for a file that holds a secret (a token, the Torznab servers file): no group or other
    permission bit at all (mode 600 or stricter)."""
    return not (mode & 0o077)


def read_token(name: str) -> str:
    path = CONFIG_DIR / f"{name}-token"
    if not path.exists():
        raise StageError("config.token", f"missing {home_shown(str(path))}",
                         f"run: omarchy-anime auth {name} or write the token to {home_shown(str(path))} with mode 600")
    mode = path.stat().st_mode & 0o777
    if not is_private_mode(mode):
        raise StageError("config.token", f"{home_shown(str(path))} has mode {mode:o}; refusing to read a "
                         "world/group-readable token", f"chmod 600 {home_shown(str(path))}")
    return path.read_text(encoding="utf-8").strip()


# ----------------------------------------------------------------------------- token files
#
# One place that writes a token, one place that removes one, so the mode ordering cannot drift.
# The secret must never exist on disk at a wider mode, not even for an instant, so the file is
# created at 0o600 by `tempfile.mkstemp` itself rather than chmod-ed after.

# A temp token that a SIGKILL left behind is swept by the next save once it is this old (a younger
# one may be another process's save in flight), and by sign-out at any age.
TOKEN_TEMP_STALE_S = 60


def _token_temps(name: str) -> list[Path]:
    """Temp files `write_token` makes for this token (`.<name>-token.tmp.<random>`)."""
    try:
        return sorted(CONFIG_DIR.glob(f".{name}-token.tmp.*"))
    except OSError:
        return []


def _sweep_token_temps(name: str, older_than: float | None) -> None:
    for tmp in _token_temps(name):
        try:
            if older_than is None or time.time() - tmp.lstat().st_mtime > older_than:
                tmp.unlink()
        except OSError:
            pass


def write_token(name: str, value: str) -> Path:
    """Atomic: the token goes to a temp file in the same folder, created at 0o600 by
    `tempfile.mkstemp` (`O_EXCL`, `O_NOFOLLOW`, a random name), is fsync-ed, then `os.replace`s the
    old one. A kill or a full disk mid-write leaves the old token whole, never an empty or
    half-written one. A temp that a SIGKILL left behind is swept here once stale and by
    `remove_token`, and the scrubber reads it like a token (`known_secrets`)."""
    _sweep_token_temps(name, TOKEN_TEMP_STALE_S)
    path = write_private(CONFIG_DIR / f"{name}-token", str(value).strip())
    forget_secrets()                  # the scrubber has to know about a token the moment it exists
    return path


def write_private(path: Path, text: str) -> Path:
    """The atomic write for a file that holds a secret, in the config folder: a temp file beside it
    (`.<name>.tmp.<random>`), created at 0o600 by `tempfile.mkstemp` itself (`O_EXCL`, `O_NOFOLLOW`), fsync-ed,
    then `os.replace`d over the old one, and the folder fsync-ed. A kill or a full disk mid-write leaves the old
    file whole; a failure leaves no temp copy. Used by `write_token` and `torznab_servers.write`; the caller
    tells the scrubber (`forget_secrets`)."""
    import tempfile
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    set_private(path.parent, 0o700)      # an older build created it at the umask default
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.tmp.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:                # Ctrl-C included: no temp copy of the secret is left behind
        tmp.unlink(missing_ok=True)
        raise
    try:                                 # make the rename itself durable; not every filesystem allows it
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
    return path


def remove_token(name: str) -> tuple[bool, Path]:
    """Idempotent: signing out of a provider you were never signed in to is not an error.
    A temp copy a killed save left behind goes too: sign-out leaves no copy of the token."""
    path = CONFIG_DIR / f"{name}-token"
    existed = path.exists()
    path.unlink(missing_ok=True)
    _sweep_token_temps(name, None)
    forget_secrets()
    return existed, path


def token_mode(name: str) -> str | None:
    path = CONFIG_DIR / f"{name}-token"
    try:
        return f"{path.stat().st_mode & 0o777:o}"
    except OSError:
        return None


def redact(value: str) -> str:
    """Mask a secret for display: the **last** four characters only.

    The first four are worse than useless — a JWT's are its header segment (`eyJ0…`), which
    fingerprints the algorithm while leaking a real prefix of the secret. The tail identifies
    "which of my two tokens is this?" without helping anyone guess it.
    """
    if not value:
        return ""
    if len(value) <= 4:
        return "…"          # four characters of a five-character secret is the whole secret
    return "…" + value[-4:]


# ----------------------------------------------------------------------------- redaction
#
# Redaction happens at the SINKS — Log.emit, the CLI's stdout, the trace renderer — and not at the
# call sites. The leak that motivated this went out through an mpv IPC *reply*, a value nobody had
# thought of as carrying a secret; a per-call-site `url.replace(self.token, "…")` protects exactly
# the line its author was thinking about. Scrubbing where bytes are written covers every field,
# including the ones added after this comment.

REDACTED = "<redacted>"

# Query parameters whose value is a credential. TorBox's own CDN URL carries `?token=<api key>`
# (measured 2026-09-16) and that is the one that reached the user's log.
SECRET_QUERY_KEYS = ("token", "apikey", "api_key", "api-key", "access_token", "accesstoken",
                     "access-token", "auth", "auth_token", "authorization", "key", "password",
                     "passwd", "pwd", "secret", "client_secret", "sig", "signature", "session",
                     "device_code",   # TorBox's device code redeems the account token
                     "passkey", "jackett_apikey")   # a tracker's passkey; Jackett's download links
# The parameter name must start immediately after the separator, so `monkey=` is not `key=`.
_SECRET_QUERY_RE = re.compile(r"(?i)([?&;](?:" + "|".join(SECRET_QUERY_KEYS) + r")=)([^&\s\"'\\]+)")

# The user info of a URL (`scheme://user:password@host`), which a hand-written address can carry. Only
# right after `://`, so an e-mail address in ordinary text is left alone. The scheme is at most 64 characters: an
# unbounded one was retried from every word boundary of a run (`a.a.a.…`) to its end, quadratic on a long release name
# (64,000 characters: 11 s per log line). Each start now reads at most 64 characters past it.
_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,63}://)[^/?#\s@\"'<>]+@")

# Flags whose *next* argv element is a secret. `--flag=value` is handled as well.
SECRET_ARG_FLAGS = ("--token", "--client-secret", "--password", "--api-key", "--apikey", "--secret")

# The shortest prefix of a stored token we will match on. `Mpv.command` logs `str(data)[:120]`, so a
# truncated token is the normal case; matching a prefix plus its token-alphabet tail catches both.
_SECRET_PREFIX_LEN = 16
_TOKEN_CHARS = r"[A-Za-z0-9._~+/=-]"

# A stored token shorter than this is masked as a whole token only (`_secret_pattern`): a prefix that short would
# match ordinary words (`tok` in `token`). And a sign-in shorter than this is refused before it is sent
# (`cli.save_verified_token`). The shortest real format, measured on stored keys: 32 characters (an OpenSubtitles key;
# Subdl's 49, TorBox's UUID 36, an AniList JWT about 1,000; Real-Debrid's unverified). 16 leaves a margin of 2.
TOKEN_MIN_LEN = _SECRET_PREFIX_LEN

# The needle list and what it was built from (`_secret_pattern`), as one tuple so a thread never pairs one's pattern
# with another's signature; None until built. Rebuilt when the config folder or a file it read changed, so a token
# another process saved during this run (a sign-in while `watch` or a long play runs) is masked.
_secrets_state: "tuple | None" = None
# Secrets this process holds that no file holds yet (a Torznab key being checked before it is saved).
_held_secrets: list[str] = []


def forget_secrets() -> None:
    """Drop the cached token list (the tests redirect CONFIG_DIR; a sign-in adds a token)."""
    global _secrets_state
    _secrets_state = None


def remember_secret(value: str) -> None:
    """Tell the scrubber about a secret before it is written anywhere (a Torznab server's key is checked
    against the server, and a pasted token is checked with its provider, before a file holds it). For this process
    only."""
    value = str(value or "").strip()
    if value and value not in _held_secrets:
        _held_secrets.append(value)
        forget_secrets()


def known_secrets() -> list[str]:
    """Every token stored in `~/.config/omaanime/*-token`, and in a temp copy a killed save left
    behind (`.<name>-token.tmp.*`), read once per process; and every API key in the Torznab
    servers file and its temp copies (`torznab_servers.keys`, the file's one owner). A short one is kept: it is
    masked as a whole token (`_secret_pattern`).

    Deliberately **not** `read_token`: that one refuses a group/world-readable file, which is the
    right call for *using* a token and the wrong one for hiding it. A token at mode 644 is the case
    where scrubbing matters most.
    """
    from . import torznab_servers                      # the Torznab servers' API keys (lazy: it imports core)
    out = [k for k in torznab_servers.keys() if k] + list(_held_secrets)
    for path in _secret_files():
        try:
            value = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if value:
            out.append(value)
    return out


def _secret_files() -> list[Path]:
    """The token files `known_secrets` reads: `*-token`, and a temp copy a killed `write_token` left behind."""
    try:
        return sorted(CONFIG_DIR.glob("*-token")) + sorted(CONFIG_DIR.glob(".*-token.tmp.*"))
    except OSError:
        return []


def _stat_sig(path) -> tuple | None:
    try:
        st = os.stat(path)
        return st.st_ino, st.st_mtime_ns, st.st_size
    except OSError:
        return None


def _files_sig(paths: list[str]) -> tuple:
    return tuple(_stat_sig(p) for p in paths)


def _secret_pattern():
    """The needle list, rebuilt when the config folder changed (a token saved, which is a rename, or removed) or a
    file it read was edited in place (by hand, or by an older build): one `stat` of the folder and of each file per
    call, about 15 µs more a line with six stored secrets. A `stat` of a missing file costs an exception, so only the
    files that exist are checked: one appearing changes the folder."""
    global _secrets_state
    state = _secrets_state                  # one read: `forget_secrets` may run in another thread meanwhile
    if state is not None:
        (folder, files, paths), pattern = state
        if folder == (str(CONFIG_DIR), _stat_sig(CONFIG_DIR)) and _files_sig(paths) == files:
            return pattern
    from . import torznab_servers
    folder = (str(CONFIG_DIR), _stat_sig(CONFIG_DIR))
    paths = [str(p) for p in (*_secret_files(), torznab_servers.path()) if p.exists()]
    files = _files_sig(paths)
    needles = []
    for value in sorted(set(known_secrets()), key=len, reverse=True):
        if len(value) >= _SECRET_PREFIX_LEN:
            needles.append(re.escape(value[:_SECRET_PREFIX_LEN]) + _TOKEN_CHARS + "*")
        else:                            # a whole word only: `tok` must not mask `token`, but `key tok.` is masked
            needles.append(f"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])")
    pattern = re.compile("|".join(needles)) if needles else None
    _secrets_state = ((folder, files, paths), pattern)
    return pattern


def redact_text(text: str) -> str:
    """Scrub credentials out of one line of output, keeping everything else readable.

    A redacted URL keeps its scheme, host, path and non-secret parameters, because a trace whose
    URLs are `<redacted>` cannot answer "which host answered, and with what?".
    """
    return _redact(text, _secret_pattern()) if text else text


def redactor():
    """`redact_text` for a bulk read (`trace` reading two days of log, `trace --tail`): the stored tokens are checked
    once, now, for every line it is given. Per line, the check doubled `trace last` over 220,819 lines (4.4 s to
    8.0 s)."""
    pattern = _secret_pattern()
    return lambda text: _redact(text, pattern) if text else text


def _redact(text: str, pattern) -> str:
    out = _SECRET_QUERY_RE.sub(lambda m: m.group(1) + REDACTED, text)
    out = _USERINFO_RE.sub(lambda m: m.group(1) + REDACTED + "@", out)
    if pattern is not None:
        out = pattern.sub(REDACTED, out)
    return out


# The characters a terminal acts on: the controls below space (ESC starts CSI and OSC; OSC 52 writes the clipboard;
# CR overwrites the line), DEL, and U+0080-U+009F (U+009B is a CSI on its own to some terminals). Newline and tab are
# kept where text keeps lines. Arabic, its direction marks and no-break spaces are ordinary text, so `str.isprintable`
# is not the rule.
_TEXT_CONTROLS_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_FIELD_CONTROLS_RE = re.compile("[\x00-\x1f\x7f-\x9f]")
# In JSON text: what `json.dumps(ensure_ascii=False)` leaves raw (DEL, U+0080-U+009F), and any control below space but
# the whitespace JSON allows.
_JSON_CONTROLS_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def escape_controls(text: str, lines: bool = True) -> str:
    """Text for a terminal, with each control character shown as `\\xNN`: one a service wrote into an error or a
    reason (`trace`'s text, the device sign-in's sentence) is printed, never acted on. `lines=False` escapes
    newline and tab too (one field of a row)."""
    if not text:
        return text
    return (_TEXT_CONTROLS_RE if lines else _FIELD_CONTROLS_RE).sub(lambda m: f"\\x{ord(m.group()):02x}", text)


def json_controls(text: str) -> str:
    """JSON text with DEL and U+0080-U+009F (and any stray control below space) written as `\\u00NN`: still the same
    JSON, decoded to the same value, but nothing a terminal acts on reaches it raw."""
    return _JSON_CONTROLS_RE.sub(lambda m: f"\\u{ord(m.group()):04x}", text) if text else text


def home_shown(text: str) -> str:
    """`text` with the home folder written `~`, for what a user is asked to paste (a trace, a suggest): the path
    names their user name. Only `<home>/`, so `/home/u2` is not `~2`."""
    try:
        home = str(Path.home()).rstrip("/")
    except RuntimeError:
        return text
    return text.replace(home + "/", "~/") if text and home else text


_JWT_RE = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")
_OPAQUE_RE = re.compile(r"^[A-Za-z0-9_~+=.-]{40,}$")


def looks_secret(value: str) -> bool:
    """A bare argument that is a JWT, or 40+ characters of opaque token alphabet (no `/`, no space,
    no `:`), is treated as a secret. Nothing this CLI legitimately takes has that shape."""
    return bool(_JWT_RE.match(value) or _OPAQUE_RE.match(value))


def safe_argv(argv) -> list[str]:
    """`sys.argv[1:]`, with any credential masked — what `cli/start` logs."""
    out: list[str] = []
    mask_next = False
    for raw in argv or []:
        arg = str(raw)
        if mask_next:
            # `-` means "read it from stdin" and is not a secret. Masking it would hide the one
            # thing the log should say: that the safe form was used.
            out.append(arg if arg == "-" else REDACTED)
            mask_next = False
            continue
        if arg in SECRET_ARG_FLAGS:
            out.append(arg)
            mask_next = True
            continue
        head, sep, _ = arg.partition("=")
        if sep and head in SECRET_ARG_FLAGS:
            out.append(f"{head}={REDACTED}")
            continue
        out.append(REDACTED if looks_secret(arg) else redact_text(arg))
    return out


# ----------------------------------------------------------------------------- http

# The longest `Retry-After` `Http` sleeps before retrying. A longer ask is not slept: the status
# goes back to the caller, so a server's "come back in an hour" never stalls a command.
RETRY_AFTER_CAP_S = 10

# The methods `Http` may send again after the request went out and its answer was lost
# (RFC 9110 §9.2.2). A POST is not among them: a TorBox or Real-Debrid add, an AniList mutation or an
# OpenSubtitles download may already have happened, and sending it again could do it twice.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

# The most bytes `Http.request` reads of one answer (or of a redirect's body) when the call names no `max_bytes`:
# a service that keeps answering must not fill the memory. The largest recorded answer is an ani.zip mapping of
# 1,181 episodes, 1.94 MB; 16 MiB is 8.6 times that.
DEFAULT_MAX_BYTES = 16 * 1024 * 1024
# For an answer that grows with the viewer's account: AniList's lists (about 2.6 KB an entry: some 25,000 entries)
# and TorBox's whole `mylist` (about 7.7 KB a torrent, 1000 to a page: about 7.7 MB).
LARGE_MAX_BYTES = 64 * 1024 * 1024


def get_capped(url: str, cap: int, timeout: float) -> tuple[int, bytes, dict]:
    """GET with the response body capped **while reading**, not after. Raises StageError past the cap,
    and urllib's own errors (URLError, TimeoutError, OSError) when the host does not answer.

    A file named by a third party must not be read whole before anyone can object: a host that starts
    answering a gigabyte would be in memory first. `read_capped` checks `Content-Length` first (measured
    present on the storage host) and stops reading the moment the cap is crossed either way. (`Http.request`
    reads every answer through the same `read_capped`, to `max_bytes` or `DEFAULT_MAX_BYTES`.)
    """
    import urllib.request                               # loaded by the calls that need it, not by `status`
    from http.client import IncompleteRead
    errors = _http_errors()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    req.oma_cap, req.oma_same_origin = cap, carries_secret(url)
    try:
        resp = guarded_urlopen()(req, timeout=timeout)
    except errors.TooLarge as big:                      # a redirect's own body, read to the cap
        raise StageError("http.capped", str(big), "the file is larger than the cap; skipped", reason="too_large",
                         status=big.status)
    except errors.RedirectRefused as moved:
        raise StageError("http.capped", str(moved), "the redirect was not followed", reason=moved.reason,
                         status=moved.code, to=moved.to)
    with resp:
        try:
            body = read_capped(resp, cap, resp.headers)
        except errors.TooLarge as big:
            raise StageError("http.capped", str(big), "the file is larger than the cap; skipped", reason="too_large",
                             status=resp.status)
        except IncompleteRead as short:
            # http.client's own chunked read raises it too, with no `expected`: the chunked body ended early.
            raise StageError("http.capped", f"the body ended after {len(short.partial)} bytes, "
                             + (f"{short.expected} short of its Content-Length" if short.expected is not None
                                else "the chunked body ended early"),
                             "the connection closed before the whole file came; nothing was kept",
                             reason="answer_lost")
        return resp.status, body, {k.lower(): v for k, v in resp.headers.items()}


_LAZY_LOCK = threading.RLock()


def _once(build):
    """`build()` made once, on the first call, under `_LAZY_LOCK`. `functools.cache` holds no lock while it builds, so
    two first calls in a fan-out (`fetch_many`'s workers) each got their own error classes, and an `except
    errors.TooLarge` holding the other set missed: a refusal was retried as unreachable (measured: 84 of 150
    trials at a 1 µs switch interval). Re-entrant, as `_guarded_opener` builds `_redirect_guard`. The value sits on
    the function and the build is read through `__wrapped__`, so a test can restore one and stall the other."""
    @functools.wraps(build)
    def once():
        with _LAZY_LOCK:
            if "value" not in once.__dict__:
                once.value = once.__wrapped__()
            return once.value
    return once


@_once
def _http_errors():
    """`TooLarge`, `RedirectRefused` and `CrossOriginRedirect` (`core.TooLarge` and so on), made on first use. They
    are `URLError`s, so a bare `urlopen`'s `except URLError` or `except OSError` (`providers.range_check`, an add-on's
    own) still holds when the guard refuses a redirect; and importing `core` loads no `urllib.error` (the `status`
    poll's CPU). Inside `core` they are named through this function: a bare name never reaches the module's
    `__getattr__`."""
    import types
    import urllib.error

    class Refusal(urllib.error.URLError):
        def __str__(self):              # URLError's is "<urlopen error {reason}>", and `reason` is a code here
            return str(self.args[0])

    class TooLarge(Refusal):
        """A body read through `read_capped` went past its cap (declared, or while reading). `status` is the
        redirect's code when the body was a 3xx one (`_GuardedRedirects`), else None."""

        def __init__(self, cap: int, declared: int | None = None):
            super().__init__(f"declared {declared} bytes, cap is {cap}" if declared is not None
                             else f"body exceeded {cap} bytes while reading")
            self.cap, self.declared, self.status = cap, declared, None

    class RedirectRefused(Refusal):
        """A redirect `_GuardedRedirects` does not follow. `to` is the target's origin only (scheme, host, port): its
        path and query may carry a key. `reason` is the `StageError` reason the caller sees."""

        def __init__(self, code: int, to: str, why: str, reason: str, text: str = ""):
            super().__init__(text or f"a {code} redirect to {to} was refused: {why}")
            self.code, self.to, self.reason = code, to, reason

    class CrossOriginRedirect(RedirectRefused):
        """A redirect to another origin, refused because the request carries a key
        (urllib copies every header, and a key in the query, to whatever host a `Location` names)."""

        def __init__(self, code: int, to: str):
            super().__init__(code, to, "", "cross_origin_redirect",
                             f"a {code} redirect to another origin ({to}) was refused")

    for cls in (TooLarge, RedirectRefused, CrossOriginRedirect):
        cls.__qualname__ = cls.__name__
    return types.SimpleNamespace(TooLarge=TooLarge, RedirectRefused=RedirectRefused,
                                 CrossOriginRedirect=CrossOriginRedirect)


def url_origin(url: str) -> tuple[str, str, int | None]:
    """(scheme, host, port) with the scheme's default port filled in, lower-cased."""
    p = urllib.parse.urlsplit(url)
    scheme = (p.scheme or "").lower()
    try:
        port = p.port
    except ValueError:
        port = None
    return scheme, (p.hostname or "").lower(), port or {"http": 80, "https": 443}.get(scheme)


def origin_label(url: str) -> str:
    """`scheme://host:port` of a URL, never its user info, path or query (a log may carry it)."""
    scheme, host, port = url_origin(url)
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


def _https_upgrade(old: str, new: str) -> bool:
    """`new` is `old`'s own host moved from http to https, on the same port or from 80 to 443:
    the key stays with that host, and is encrypted from then on."""
    (s1, h1, p1), (s2, h2, p2) = url_origin(old), url_origin(new)
    return s1 == "http" and s2 == "https" and h1 == h2 and bool(h1) and (p1 == p2 or (p1, p2) == (80, 443))


# A request header whose name says it carries a credential (`Authorization`, `X-Api-Key`, `Api-Key`, `Cookie`, ...).
_SECRET_HEADER_RE = re.compile(r"(?i)auth|cookie|key|token|secret|passw|session")


def carries_secret(url: str, headers: dict | None = None, data: bytes | None = None) -> bool:
    """The request holds a credential: a header named for one, or anything the log scrubber masks (a secret query
    key, user info, a stored token or key) in its URL, a header value or its body. Such a request never follows a
    redirect to another origin (`_GuardedRedirects`). urllib sends no body on a redirect; the body is read anyway,
    so the rule does not depend on that."""
    if redact_text(url) != url:
        return True
    for name, value in (headers or {}).items():
        value = str(value)
        if _SECRET_HEADER_RE.search(str(name)) or redact_text(value) != value:
            return True
    if data:
        text = "&" + (data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data))
        return redact_text(text) != text
    return False


# Host names that name this machine or the local network (RFC 6761 `localhost`, RFC 6762 `.local`, RFC 8375
# `.home.arpa`, the common `.lan`/`.internal`), never a public image or stream host. A single-label name is local too.
_LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home.arpa", ".internal")


def public_https_refusal(url: str) -> str | None:
    """Why `url` is not an https address on a public host, or None: the one rule for covers and stills, every
    `Http.request(public_only=True)` and its redirects, and a provider's stream link (`providers.stream_refusal`).
    Refused: another scheme; a host that is not plain ASCII or holds `%` (mpv reads `%31%32%37.0.0.1` and fullwidth
    digits as 127.0.0.1, urllib the first, though neither parser below does); `localhost`, the local suffixes and a
    single-label name; an IP address that is not global, or is multicast, in any form the C library reads (`127.1`,
    `2130706433`, `0x7f.1`, `[::1]`). A public name that resolves to a local address is not caught here: https stops it
    at the certificate, before the request is sent, in urllib and in mpv (the launcher's
    `--tls-verify=yes`)."""
    import ipaddress
    import socket
    try:
        p = urllib.parse.urlsplit(str(url or ""))
        raw = p.hostname or ""
    except ValueError:
        return "the address does not parse"
    if (p.scheme or "").lower() != "https":
        return f"scheme {p.scheme or 'none'!r} is not https"
    if not raw.isascii() or "%" in raw:
        return "the host is not a plain name or address"
    host = raw.rstrip(".").lower()
    if not host:
        return "no host"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            ip = ipaddress.IPv4Address(socket.inet_aton(host))
        except (OSError, ValueError):
            ip = None
    if ip is not None:
        mapped = getattr(ip, "ipv4_mapped", None) or ip
        return None if mapped.is_global and not mapped.is_multicast else f"host {host} is not a public address"
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES) or "." not in host:
        return f"host {host} is a local name"
    return None


def _redirect_refusal(req, code: int, newurl: str) -> RedirectRefused | None:
    """Why `_GuardedRedirects` does not follow `req` to `newurl`, or None. The scheme rule holds for every request;
    the origin and public-host rules only for one the plugin's client made (`Http.request`, `get_capped`), which
    sets `oma_same_origin` and `oma_public`. A bare `urlopen` (`providers.range_check` on a stream link that carries
    its token) follows its hop: mpv and ffprobe open that link and follow the same redirect anyway."""
    errors = _http_errors()
    to = origin_label(newurl)
    if url_origin(newurl)[0] not in ("http", "https"):
        return errors.RedirectRefused(code, to, "only http and https are followed", "redirect_scheme")
    if getattr(req, "oma_public", False):
        why = public_https_refusal(newurl)
        if why:
            return errors.RedirectRefused(code, to, why, "not_public")
    if (getattr(req, "oma_same_origin", False)
            and url_origin(newurl) != url_origin(req.full_url) and not _https_upgrade(req.full_url, newurl)):
        return errors.CrossOriginRedirect(code, to)
    return None


@_once
def _redirect_guard() -> type:
    """The redirect handler of the opener `guarded_urlopen` installs, made on first use: its base class is urllib's,
    and `urllib.request` (with `http.client` and `ssl`) is about a quarter of the `status` poll's CPU.
    `core._SameOriginRedirects` still names it (`__getattr__`)."""
    import urllib.request

    class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
        """Every redirect urllib follows in this process: only to http(s); for the plugin's own client, a request
        that carries a secret (`carries_secret`) or asked `same_origin` only within its origin, or to https on the
        same host, and a `public_only` one only to https on a public host (`public_https_refusal`). The 3xx body,
        which urllib reads whole, is read to the request's cap first (`DEFAULT_MAX_BYTES` for a bare `urlopen`)."""

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            refused = _redirect_refusal(req, code, newurl)
            if refused is not None:
                try:
                    fp.close()
                except Exception:  # noqa: BLE001 - the refusal matters, not the socket
                    pass
                raise refused
            try:
                read_capped(fp, getattr(req, "oma_cap", DEFAULT_MAX_BYTES), headers)
            except _http_errors().TooLarge as big:
                big.status = code                   # the body was the redirect's, not an answer's
                raise
            new = super().redirect_request(req, fp, code, msg, headers, newurl)
            for name in ("oma_cap", "oma_same_origin", "oma_public"):
                if new is not None and hasattr(req, name):
                    setattr(new, name, getattr(req, name))
            return new

    return _GuardedRedirects


@_once
def _guarded_opener():
    import urllib.request
    return urllib.request.build_opener(_redirect_guard())


def guarded_urlopen():
    """`urllib.request.urlopen`, with `_GuardedRedirects` in the opener it uses. urlopen stays the call site (the
    tests and the add-ons' tests fake it there); the opener is installed again if anything replaced it."""
    import urllib.request
    opener = _guarded_opener()
    if getattr(urllib.request, "_opener", None) is not opener:
        urllib.request.install_opener(opener)
    return urllib.request.urlopen


def __getattr__(name: str):
    if name == "_SameOriginRedirects":
        return _redirect_guard()
    if name in ("TooLarge", "RedirectRefused", "CrossOriginRedirect"):
        return getattr(_http_errors(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def read_capped(fp, cap: int, headers=None) -> bytes:
    """Read a response (or an `HTTPError`, which is one) to at most `cap` bytes: `Content-Length` first,
    then `read(cap + 1)` in 64 KiB chunks, so a body past the cap is never held whole. Raises `TooLarge`.
    The one capped read: `get_capped`, every `Http.request` and a redirect's body all use it.

    A body that ends before its `Content-Length` raises `http.client.IncompleteRead`. `read(n)` returns
    short at a clean EOF instead of raising (a bare `read()` raises), so a cut answer used to come back as a
    whole one. The response's own `length` (the bytes still owed) is read, not the header: a HEAD, 204 or 304
    carries a `Content-Length` and no body, and its `length` is 0."""
    try:
        declared = int((headers or {}).get("Content-Length") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > cap:
        raise _http_errors().TooLarge(cap, declared)
    buf = bytearray()
    while len(buf) <= cap:
        chunk = fp.read(min(65536, cap + 1 - len(buf)))
        if not chunk:
            break
        buf += chunk
    if len(buf) > cap:
        raise _http_errors().TooLarge(cap)
    owed = getattr(fp, "length", None)
    if isinstance(owed, int) and owed > 0:
        from http.client import IncompleteRead
        raise IncompleteRead(bytes(buf), owed)
    return bytes(buf)


class Http:
    def __init__(self, log: Log, timeout: int):
        self.log, self.timeout = log, timeout

    def get_capped(self, component: str, url: str, cap: int, timeout: float | None = None) -> tuple[int, bytes, dict]:
        """`get_capped` with this client's log: one `http` line with status, bytes and time, or the
        failure. No retries: a capped download is a file, and its caller decides whether to try again.
        `timeout` defaults to the client's own."""
        t0 = time.monotonic()
        try:
            status, body, headers = get_capped(url, cap, self.timeout if timeout is None else timeout)
        except Exception as exc:
            why = exc.data if isinstance(exc, StageError) else {}      # a refusal's reason, status and target origin
            self.log.warn(component, "http", result="FAIL", ms=(time.monotonic() - t0) * 1000, method="GET",
                          url=url, cap=cap, error=f"{type(exc).__name__}: {exc}",
                          **{k: why[k] for k in ("reason", "status", "to") if why.get(k) is not None})
            raise
        self.log.debug(component, "http", result="PASS", ms=(time.monotonic() - t0) * 1000, method="GET", url=url,
                       status=status, bytes=len(body), cap=cap)
        return status, body, headers

    def _backoff(self, component: str, shown: str, attempt: int, why: str, delay: float | None = None) -> None:
        """Wait before the next attempt: `delay` (a server's `Retry-After`) or 1.5^attempt s, plus up to
        0.5 s of jitter. The wait is logged before it starts, so a trace shows where the time went."""
        wait = (1.5 ** attempt if delay is None else delay) + random.uniform(0, 0.5)
        self.log.info(component, "retry", result="RETRY", url=shown, attempt=attempt, next_attempt=attempt + 1,
                      reason=why, wait_s=round(wait, 2))
        time.sleep(wait)

    def request(self, component: str, method: str, url: str, *, headers: dict | None = None,
                data: bytes | None = None, retries: int = 3, expect_json: bool = True,
                log_url: str | None = None, max_retry_after: float = RETRY_AFTER_CAP_S,
                timeout: float | None = None, expected: tuple[int, ...] = (), idempotent: bool | None = None,
                max_bytes: int | None = None, raw_body: bool = False, same_origin: bool = False,
                log_body: bool = True, public_only: bool = False):
        """One external call, logged with params, status, duration. Retries 429/5xx with jittered backoff;
        a `Retry-After` over `max_retry_after` is not waited for. `timeout` overrides the
        client's socket timeout for this call (the caller's own bound, e.g. TorBox's torrent info).
        `expected` names HTTP error statuses that are a normal answer for this call (TorBox's 400
        "not approved yet" while a device sign-in waits): they are logged at debug as PASS, so every
        FAIL line in a trace is a real failure, and `trace` never names a normal answer `unrecovered:`.

        An answer lost after the request went out (the connection dropped or reset, a
        truncated body, a read timeout) and a 2xx body that is not JSON end as a `StageError` too,
        never a raw exception. A lost answer is sent again only when the call is idempotent, since the
        service may already have acted on it: a POST fails at once. `idempotent` says so for a call
        whose method does not (an AniList read query is a POST); None decides by `IDEMPOTENT_METHODS`.
        A body that is not JSON is never sent again: it is a whole answer from something that is not
        the API. A connect or send failure (`URLError`) is retried for every method: nothing arrived.

        Bounds and opt-ins:
        - `max_bytes`: the success body, the HTTP-error body and a redirect's body are each read to at most this
          many bytes (`read_capped`), `DEFAULT_MAX_BYTES` when not given; past it the call ends as a `StageError`
          (`reason="too_large"`), not retried. A body nested past Python's recursion limit is not JSON.
        - `raw_body`: an HTTP-error body comes back as bytes, as a success body does with `expect_json=False`
          (a Torznab `<error>` arrives on 400/410/429 too).
        - `same_origin`: a redirect to another origin is refused (`reason="cross_origin_redirect"`). A request that
          carries a secret (`carries_secret`) gets this without asking.
        - `public_only`: the URL and every redirect must be https on a public host (`public_https_refusal`), else
          `reason="not_public"` before anything is sent. Any request: only http(s) URLs (`reason="scheme"`), and
          only http(s) redirects (`_GuardedRedirects`).
        Error text never shows the raw `url` when `log_url` is given: urllib's exception text is masked.
        `log_body=False` logs an HTTP error body's length (`bytes`), never its first 300 bytes, for a call
        whose error bodies may carry what the scrubber does not know (Prowlarr's paths, an indexer's settings)."""
        # Imported here, not at the top: `status` runs every ten seconds and never makes a request.
        import urllib.error
        import urllib.request
        from http.client import HTTPException, InvalidURL
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        shown = log_url or url

        def target(u: str) -> str:
            p = urllib.parse.urlsplit(u)
            return p.path + (f"?{p.query}" if p.query else "")
        url_target, shown_target = target(url), target(shown)

        def masked(text: str) -> str:
            """Exception text with the raw URL, and its request target (`/path?query`, which is what
            `http.client.InvalidURL` quotes), shown as they are logged."""
            if shown == url or not url:
                return text
            text = text.replace(url, shown)
            return text.replace(url_target, shown_target) if url_target and url_target != shown_target else text

        def too_large(big: TooLarge, ms: float, attempt: int, status) -> StageError:
            self.log.warn(component, "http", result="FAIL", ms=ms, method=method, url=shown, status=status,
                          attempt=attempt, reason="too_large", cap=big.cap, declared=big.declared, retry=False,
                          suggest="the answer is larger than this call accepts; it was not read whole")
            return StageError(f"{component}.http", f"{method} {shown} answered more than {big.cap} bytes",
                              "the answer is larger than this call accepts", reason="too_large", status=status)

        scheme = urllib.parse.urlsplit(url).scheme.lower()
        refusal = (f"scheme {scheme or 'none'!r} is not http(s)" if scheme not in ("http", "https")
                   else public_https_refusal(url) if public_only else None)
        if refusal:
            reason = "scheme" if scheme not in ("http", "https") else "not_public"
            self.log.warn(component, "http", result="FAIL", method=method, url=shown, reason=reason, error=refusal,
                          retry=False, suggest="the address is not one this call fetches; nothing was sent")
            raise StageError(f"{component}.http", f"{method} {shown}: {refusal}",
                             "the address is not one this call fetches", reason=reason)
        cap = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
        keyed = bool(same_origin) or carries_secret(url, hdrs, data)
        errors = _http_errors()
        opener = guarded_urlopen()
        if idempotent is None:
            idempotent = method.upper() in IDEMPOTENT_METHODS
        for attempt in range(1, retries + 1):
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            req.oma_cap, req.oma_same_origin, req.oma_public = cap, keyed, public_only
            t0 = time.monotonic()
            lost = None
            # Only the network is inside this `try`. A log line that fails (a closed stderr, a full
            # disk) must never read as a lost answer, nor send the request again.
            try:
                with opener(req, timeout=self.timeout if timeout is None else timeout) as resp:
                    body = read_capped(resp, cap, resp.headers)
                    status, answered = resp.status, dict(resp.headers)
            except errors.TooLarge as big:                  # the answer's body, or a redirect's (`big.status`)
                raise too_large(big, (time.monotonic() - t0) * 1000, attempt, big.status or 200)
            except InvalidURL as bad:
                # Raised before anything is sent (a space or control character in the address): not an answer
                # lost, and asking again cannot help.
                self.log.warn(component, "http", result="FAIL", ms=(time.monotonic() - t0) * 1000, method=method,
                              url=shown, attempt=attempt, reason="invalid_url", retry=False,
                              error=masked(f"{type(bad).__name__}: {bad}"),
                              suggest="the address holds a character a URL cannot carry; fix the address")
                raise StageError(f"{component}.http", f"{method} {shown}: the address cannot be sent "
                                 f"({masked(str(bad))})", "fix the address", reason="invalid_url")
            except errors.RedirectRefused as moved:
                suggest = ("the server sent this request to another host; with a key attached it is not followed. "
                           "Use the address the server itself answers on" if moved.reason == "cross_origin_redirect"
                           else "the server redirected to an address this call does not fetch; not followed")
                self.log.warn(component, "http", result="FAIL", ms=(time.monotonic() - t0) * 1000, method=method,
                              url=shown, status=moved.code, attempt=attempt, reason=moved.reason,
                              to=moved.to, retry=False, suggest=suggest)
                raise StageError(f"{component}.http", f"{method} {shown}: {moved}",
                                 "use the address the server itself answers on"
                                 if moved.reason == "cross_origin_redirect" else "the redirect was not followed",
                                 reason=moved.reason, status=moved.code)
            except urllib.error.HTTPError as exc:
                ms = (time.monotonic() - t0) * 1000
                try:
                    body = read_capped(exc, cap, exc.headers)
                    body_error = None
                except errors.TooLarge as big:
                    raise too_large(big, ms, attempt, exc.code)
                except (OSError, HTTPException) as read_exc:     # the status came; its body did not
                    body, body_error = b"", masked(f"{type(read_exc).__name__}: {read_exc}")
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                self.log.emit("debug" if exc.code in expected else "warn", component, "http",
                              result="PASS" if exc.code in expected else "FAIL", ms=ms, method=method, url=shown,
                              status=exc.code, attempt=attempt, retry_after=retry_after,
                              **({"body": body[:300].decode("utf-8", "replace")} if log_body else {"bytes": len(body)}),
                              **({"body_error": body_error} if body_error else {}))
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                    # ASCII digits only. `"²".isdigit()` is True and `float("²")` raises, which escaped as a
                    # raw ValueError, not a status or a StageError.
                    delay = (float(retry_after) if retry_after and retry_after.isascii() and retry_after.isdigit()
                             else 1.5 ** attempt)
                    if delay <= max_retry_after:
                        self._backoff(component, shown, attempt, f"status {exc.code}", delay)
                        continue
                    # 309 digits or more read as `inf`, which `int` refuses.
                    self.log.warn(component, "retry_after_capped", result="FAIL", url=shown, status=exc.code,
                                  retry_after_s=int(delay) if math.isfinite(delay) else None, cap_s=max_retry_after,
                                  suggest="the service asked for a long wait; the caller decides what to do")
                if raw_body:
                    return exc.code, body, dict(exc.headers or {})
                try:
                    parsed = json.loads(body) if body else None
                except (ValueError, RecursionError):    # not JSON, Latin-1 that is not UTF-8, or nested too deep
                    parsed = body.decode("utf-8", "replace")
                return exc.code, parsed, dict(exc.headers or {})
            except urllib.error.URLError as exc:
                # Raised while connecting or sending (a connect timeout included): the request never
                # arrived whole, so sending it again is safe for every method.
                ms = (time.monotonic() - t0) * 1000
                self.log.warn(component, "http", result="FAIL", ms=ms, method=method, url=shown,
                              error=masked(str(exc)), attempt=attempt, retry=attempt < retries)
                if attempt < retries:
                    self._backoff(component, shown, attempt, "unreachable")
                    continue
                raise StageError(f"{component}.http", f"{method} {shown} failed: {masked(str(exc))}",
                                 "check network / the service status")
            except (OSError, HTTPException) as exc:
                # Raised after the request was sent, so urllib did not wrap it in URLError:
                # the answer never came (RemoteDisconnected, ConnectionResetError, BadStatusLine, a
                # read timeout) or came short (IncompleteRead). urllib sends `Connection: close`, so
                # this is never a stale keep-alive socket: the service had the request and may have
                # acted on it.
                lost = exc
            ms = (time.monotonic() - t0) * 1000
            if lost is not None:
                again = idempotent and attempt < retries
                error = masked(f"{type(lost).__name__}: {lost}")
                what = ("no answer came in time" if isinstance(lost, TimeoutError)
                        else "the connection dropped before the whole answer came")
                suggest = ("check network / the service status" if idempotent else
                           "the service may have acted on it: check its state before trying again")
                self.log.warn(component, "http", result="FAIL", ms=ms, method=method, url=shown, error=error,
                              attempt=attempt, reason="answer_lost", retry=again, idempotent=idempotent,
                              suggest="" if again else suggest)
                if again:
                    self._backoff(component, shown, attempt, "answer_lost")
                    continue
                raise StageError(f"{component}.http",
                                 f"{method} {shown} failed: {what} ({error})"
                                 + ("" if idempotent else "; it was not sent again"), suggest, reason="answer_lost",
                                 timed_out=isinstance(lost, TimeoutError))
            if not expect_json or not body:
                self.log.debug(component, "http", result="PASS", ms=ms, method=method, url=shown,
                               status=status, bytes=len(body), attempt=attempt)
                return status, body, answered
            try:
                parsed = json.loads(body)
            except (ValueError, RecursionError) as exc:     # not JSON, not UTF-8, or nested past the recursion limit
                # Whatever answered is not the API: a proxy, a captive portal, a login or outage page.
                # Not sent again, whatever the method: the answer came whole, and asking again gets
                # the same page a few seconds later instead of letting the caller try elsewhere.
                ctype = next((v for k, v in answered.items() if k.lower() == "content-type"), None)
                suggest = ("something other than the service answered (a proxy, a captive portal, a login "
                           "or outage page); check network / the service status")
                self.log.warn(component, "http", result="FAIL", ms=ms, method=method, url=shown,
                              status=status, bytes=len(body), attempt=attempt, reason="not_json",
                              content_type=ctype, error=f"not JSON: {type(exc).__name__}: {exc}", retry=False,
                              body=body[:300].decode("utf-8", "replace"), suggest=suggest)
                raise StageError(f"{component}.http",
                                 f"{method} {shown} answered {status} with a body that is not JSON "
                                 f"({ctype or 'no content type'})", suggest, status=status, reason="not_json")
            self.log.debug(component, "http", result="PASS", ms=ms, method=method, url=shown,
                           status=status, bytes=len(body), attempt=attempt)
            return status, parsed, answered
        raise StageError(f"{component}.http", f"{method} {shown}: retries exhausted")


def cached_json(path: Path, max_age: float | None):
    """Return cached JSON if younger than max_age seconds (None = forever).

    A cache file can go bad in more ways than "invalid JSON": non-UTF-8 bytes raise
    `UnicodeDecodeError` from `read_text` (a `ValueError` subclass, but not a `JSONDecodeError`, so
    it used to escape this function uncaught), and a race between the `exists()` check above and the
    read can raise `OSError` (the file vanishing, permissions changing). Every caller here already
    treats a falsy return as "fetch again" or "start from empty", so any read/parse failure
    degrades the same way as a missing file — never an exception.
    """
    if not path.exists():
        return None
    try:
        if max_age is not None and time.time() - path.stat().st_mtime > max_age:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_cache(path: Path, value) -> None:
    """Atomic JSON write: a temp file beside it (`private_temp`: a random name, mode 0o600, `O_EXCL`), then
    `os.replace`. Two CLI processes racing to update the same cache file (the Browse overlay's `media` and
    `episodes --fetch` start together for one detail view) never share one temp path, and a record is never
    readable by another user, not even for the instant before a chmod. A failed write leaves no temp behind."""
    text = json.dumps(value, ensure_ascii=False)        # before the temp exists: a value that is not JSON leaves none
    _write_atomic(path, text.encode("utf-8"))


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = private_temp(path)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def flocked(path: Path, log: "Log", component: str, what: str, wait_s: float = 2.0):
    """`flock` on `path` around a read-modify-write, so two writers (two processes, or two Services) never lose one's
    update. A lock that cannot be opened, or had within `wait_s`, is logged (`<component> lock FAIL`, naming `what`
    lock it is) and the body runs anyway: a write is never dropped for want of the lock."""
    try:
        private_dir(path.parent)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:                   # a read-only or full cache: the write itself then says it did not land
        log.warn(component, "lock", result="FAIL", error=str(exc), path=str(path),
                 suggest=f"{what} lock file could not be opened; writing without it")
        yield
        return
    deadline = time.monotonic() + wait_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN) or time.monotonic() >= deadline:
                    log.warn(component, "lock", result="FAIL", error=str(exc), waited=wait_s,
                             suggest=f"another writer held {what} lock; writing without it")
                    break
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)                     # closing the descriptor releases the lock


def write_bytes_atomic(path: Path, data: bytes) -> Path:
    """`write_cache` for a non-JSON payload, through the same private temp file and `os.replace`.

    mpv must never be handed a half-written subtitle file, and two CLI processes can be side-loading
    the same episode at once. A failed write leaves no temp behind.
    """
    _write_atomic(path, data)
    return path


def touch_private(path: Path) -> None:
    """Create or touch a stamp or lock file at 0o600 in a private folder (`private_dir`)."""
    private_dir(path.parent)
    path.touch(mode=0o600, exist_ok=True)
    os.utime(path, None)
