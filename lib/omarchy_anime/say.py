"""Every sentence a human reads.

The log and the viewer are different readers. The log needs stage names and counts — it is what
made 2026-09-19's investigation possible and nothing here softens it. The viewer needs a sentence.
They shared one field, so the viewer read stage names (observed 2026-09-19, from the local route
that was later removed):

    Add TorBox or Real-Debrid — Super+A then , opens Accounts — or try again later. · local:
    local.swarm no peer answered for this release in 20s; local.swarm no peer answered for this
    release in 20s | not tried (no token or disabled): torbox, realdebrid

This module is the viewer's half, and only the viewer's half. It is pure: structured outcome in,
sentence out, no I/O. That is what lets a test hold the tone as a property rather
than as a review habit — a stage added without copy fails the suite.
"""
from __future__ import annotations

from pathlib import Path

# Punctuation that means "this string was assembled by a program". No sentence may contain one.
FORBIDDEN = (" · ", " | ", "rd.rangeCheck", "rd.addMagnet", "play.stage", "  ")

# Vocabulary that belongs to the log and the CLI, never to a sentence the viewer reads. TorBox
# and Real-Debrid are products the viewer chose to sign up for, not internal terms, and are the
# deliberate exception — the banned word is the generic "debrid", not the two brand names.
JARGON = ("release", "swarm", "probe", "seeder", "seeders", "torrent", "magnet", "provider", "debrid")

# Where the settings sheet is; Accounts (its first section) and Indexers are sections of it. Named as
# a place that always exists: nothing binds Super+A (not Omarchy's defaults, and the plugin ships no
# keybind), which is what this used to name. `ServiceLogic.js` carries the same
# SETTINGS_WHERE for the shell's own lines; a test holds them equal.
SETTINGS_WHERE = "Settings in the Anime bar popout"
# The IpcHandler target in Service.qml, for the terminal half of the hint.
IPC_NAME = "omaanime"
# How to reach Accounts. The short form fits the lines the surfaces cap at 120 characters (the action
# half of a `suggest`; `cli.ACCOUNTS` is it); the long one adds the command for a terminal.
ACCOUNTS_OPEN = f"open Accounts from {SETTINGS_WHERE}"
ACCOUNTS_HOW = f"{ACCOUNTS_OPEN}, or run `omarchy-shell {IPC_NAME} browse accounts`"
# Where Accounts and the AI agent buttons are, for the empty state's hint.
AGENT_WHERE = f"{SETTINGS_WHERE} has Accounts and AI agent help"

_STAGES = {
    "start": "Getting ready",
    "search": "Looking for this episode",
    "rank": "Sorting what turned up",
    "torbox": "Checking whether TorBox already has it",
    "realdebrid": "Checking whether Real-Debrid already has it",
    "load": "Opening it in the player",
    "done": "Playing",
}

# A provider named mid-sentence, as the viewer reads it. The same
# names as `providers.PROVIDER_NAMES`, kept here so this module stays free of imports.
NEXT_NAMES = {"torbox": "TorBox", "realdebrid": "Real-Debrid"}

# The two providers a play can give up on; each carries a "stopped answering" reading.
GAVE_UP = {
    "torbox": "TorBox isn't answering for this episode",
    "realdebrid": "Real-Debrid isn't answering for this episode",
}


def _next_clause(next_provider) -> str:
    name = NEXT_NAMES.get(str(next_provider or ""))
    return f"so trying {name} instead." if name else "and nothing else is set up to try."


def stage(name: str, detail: dict | None = None) -> str:
    """The one-line sentence for a play that is still running.

    This function is the single place a `status.stage()` message comes from; that does
    not mean dropping what the viewer used to be told when a provider gave up mid-play
    — that is real information the generic phase sentence cannot carry, not
    the jargon this module exists to keep out.
    """
    detail = detail or {}
    key = str(name or "")
    text = _STAGES.get(key)
    if text is None:
        return "Working on it"
    if key == "search" and detail.get("pinDropped"):
        # The viewer's pinned pack stopped working and was let go.
        return "Your pinned pack stopped working, so looking for this episode elsewhere"
    if key == "search" and detail.get("pinUnavailable"):
        # TorBox answered the pinned pack with an error on its own side; the pin stays. The value
        # is the error's kind, so this line and the headline come from the same row of `_TORBOX_SIDE`.
        return torbox_side_kind_words(detail["pinUnavailable"])[2]
    if key == "search" and detail.get("pinStuck"):
        # The pinned pack is queued but its download has not moved for hours.
        return (f"Your pinned pack has not downloaded any further in {int(detail['pinStuck'])} hours, "
                "so looking for this episode elsewhere")
    if key in GAVE_UP and (detail.get("blocked") or detail.get("skipped")):
        return f"{GAVE_UP[key]}, {_next_clause(detail.get('nextProvider'))}"
    if key == "torbox" and detail.get("downloading") is not None:
        # The play waits for the original-audio copy TorBox is downloading in place of a cached dub.
        return f"TorBox is downloading it ({int(detail['downloading'])} %)"
    if key == "load" and detail.get("dubOnly"):
        # `dubs=avoid` found nothing but dubbed copies, so the dub plays.
        return "Only a dubbed version was found, so opening that in the player"
    return text


def no_provider() -> dict:
    """No TorBox or Real-Debrid token, so nothing can play (the plugin never streams a
    torrent itself). Names the two services, never a website."""
    return {"headline": "Playback needs TorBox or Real-Debrid.",
            "action": f"Add either one: {ACCOUNTS_HOW}.",
            "hint": ""}


# The viewer's one date style: `ServiceLogic.savedWhen`'s ("Oct 20, 2025").
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def start_date_parts(date) -> tuple[int | None, int | None, int | None]:
    """AniList's `startDate` (a FuzzyDate: year, month, day, each optional) as `(year, month, day)`, each None when
    absent or out of range. A whole float (`2027.0`) is its int, as JavaScript reads it; a bool or a string is not a
    number."""
    if not isinstance(date, dict):
        return None, None, None

    def part(key, low, high):
        value = date.get(key)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return value if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high else None

    return part("year", 1, 9999), part("month", 1, 12), part("day", 1, 31)


def start_date(date) -> str:
    """`startDate` as the viewer reads it: "Mar 14, 2027", "Mar 2027", "2027", or "" without a year.
    `BrowseModel.js:startDateText` is the same table (a shared cases file holds them equal)."""
    year, month, day = start_date_parts(date)
    if year is None:
        return ""
    if month is None:
        return str(year)
    if day is None:
        return f"{MONTHS[month - 1]} {year}"
    return f"{MONTHS[month - 1]} {day}, {year}"


def not_out(title: str, date=None) -> dict:
    """A play of an entry AniList lists as not yet released, with no airing schedule: refused before anything starts.
    The date AniList gives, when it gives one, is AniList's, and said as such."""
    when = start_date(date)
    hint = ""
    if when:
        year, month, day = start_date_parts(date)
        # A day is "on" a date; a month or a year is "in" it.
        hint = f"AniList expects it {'on' if month is not None and day is not None else 'in'} {when}."
    return {"headline": f"{title or 'This show'} is not out yet.", "action": "", "hint": hint}


# Why no indexer add-on is loaded. `indexers.Registry.reading()` computes it once; the
# sentences here, and the log's detail there, are keyed by it.
NOTHING_ADDED = "nothing_added"         # no add-on file in the folder
NONE_LOADED = "none_loaded"             # files there (or listed in the setting), none of them loaded
FOLDER_UNUSABLE = "folder_unusable"     # the folder is a file, or cannot be read

_SAYS_WHY = f"{SETTINGS_WHERE} says why, and so does the log."

# The empty state, word for word, as (headline, action). The
# play panel, the release picker and Settings' Indexers section all show these two sentences.
NOTHING_SET_UP = ("No indexer is set up.", "Add your Prowlarr or Jackett in Accounts, or add an indexer add-on.")


def shown_path(path: str) -> str:
    """A folder as a viewer reads it: `~/…` when it is under their home, the absolute path otherwise
    (the add-on folder is per user, `$XDG_CONFIG_HOME/omaanime/indexers`, and
    `/home/<user>/…` read as if it were one fixed place). For sentences only; the log and the JSON
    fields keep the absolute path."""
    try:
        rel = Path(path).relative_to(Path.home())
    except (ValueError, RuntimeError):
        return path
    return "~" if str(rel) == "." else f"~/{rel}"


def no_indexer(folder: str, reading: str, count: int = 0, torznab: bool = False) -> dict:
    """No indexer add-on loaded, so nothing can be searched (the plugin ships none).
    `reading` is one of the three constants above; `count` is how many files did not
    load. Settings' Indexers section says which loaded and why the others did not, so the last two
    readings point there and at the log. Files that did not load are "not loaded", as the sheet
    says: a Refusal is auto-play declining to choose. `torznab`: the Torznab servers
    file is among what did not load, so the headline does not call it an add-on file."""
    folder = shown_path(folder)
    if reading == FOLDER_UNUSABLE:
        return {"headline": "The folder for indexer add-ons cannot be used.",
                "action": _SAYS_WHY,
                "hint": f"Indexer add-ons go in {folder}."}
    if reading == NONE_LOADED:
        if torznab:
            headline = ("Your Torznab servers could not be used." if count == 1
                        else "None of your indexers could be used.")
        else:
            headline = ("Your indexer add-on did not load." if count == 1
                        else "None of your indexer add-ons loaded.")
        return {"headline": headline,
                "action": _SAYS_WHY,
                "hint": "Nothing can be searched until one loads."}
    # The empty state's words, then where add-ons go, and that Settings has Accounts and the two buttons that hand
    # the viewer's own AI agent a prompt (Indexers: build an add-on, set up Prowlarr). One line, within the cap.
    return {"headline": NOTHING_SET_UP[0], "action": NOTHING_SET_UP[1],
            "hint": f"Add-ons go in {folder}; {AGENT_WHERE}."}


QUEUED_ACTION = "Play again in a few minutes."
# The cached dub `dubs=avoid` set aside, named by its language and its group and height when the play stopped while
# TorBox downloads the original-audio copy. One sentence with `QUEUED_ACTION`'s words in front.
DUB_GROUP_CHARS = 12


def has_dub_cached(probes: list[dict]) -> bool:
    """Whether a TorBox row names a cached dub `dubs=avoid` set aside (`dub_cached`)."""
    return any(p.get("provider") == "torbox" and p.get("dub_cached") for p in probes or [])


def dub_clause(probes: list[dict], action: str = QUEUED_ACTION) -> str:
    """`action` (one sentence) with the clause that allowing dubs plays the cached dub now, when a TorBox row carries
    one (`dub_cached`: `{lang, group, resolution}`); `action` alone otherwise."""
    dub = next((p.get("dub_cached") for p in probes or [] if p.get("provider") == "torbox" and p.get("dub_cached")),
               None)
    if not dub:
        return action
    lang = str(dub.get("lang") or "").strip()
    group = "".join(c for c in str(dub.get("group") or "") if c.isalnum() or c in " -")[:DUB_GROUP_CHARS].strip()
    res = dub.get("resolution")
    which = " ".join(w for w in (group, f"{res}p" if res else "") if w)
    what = f"the {lang} dub" if lang else "the dub"
    return (f"{action.rstrip('.')}, or allow dubs in Settings to play {what} TorBox has"
            + (f" ({which})." if which else "."))


# ---- the wait for TorBox's download of the original-audio release ----
# TorBox's `eta` (seconds) answers 8640000 (100 days) while it has no estimate; that, a missing value and 0 or less are
# "unknown" and never shown as a number.
TORBOX_ETA_UNKNOWN = 8640000


def eta_seconds(value) -> float | None:
    """Seconds left as a number, or None when unknown. What comes in is the wait's own time left from TorBox's bytes
    (`left_s`), never TorBox's `eta`; 8640000 (100 days) stays "unknown", TorBox's own sentinel."""
    try:
        secs = float(value)
    except (TypeError, ValueError):
        return None
    return None if secs <= 0 or secs >= TORBOX_ETA_UNKNOWN else secs


def eta_words(value) -> str | None:
    """An estimate as a person says it, the form chosen from the rounded value: "under a minute"; 1-9 minutes exact
    ("about a minute", "about 4 minutes"); 10-55 minutes to the nearest 5; "about an hour" from 56 minutes to 1 hour
    4; then hours and the nearest 10 minutes ("about 1 hour 20 minutes", "about 2 hours"); "about a day" when that
    rounds to 23 hours 30 up to 24 hours 30; "more than a day" past that. None when unknown."""
    secs = eta_seconds(value)
    if secs is None:
        return None
    minutes = secs / 60

    def near(step: int) -> int:                      # half up, as a person rounds (not to the even number)
        return int(minutes / step + 0.5) * step

    if minutes < 1:
        return "under a minute"
    if minutes < 9.5:
        n = near(1)
        return "about a minute" if n == 1 else f"about {n} minutes"
    if minutes < 55.5:
        return f"about {near(5)} minutes"
    total = near(10)
    if total > 24.5 * 60:
        return "more than a day"
    if total >= 23.5 * 60:
        return "about a day"
    if total <= 60:
        return "about an hour"
    hours, rest = divmod(total, 60)
    said = f"about {hours} hour" + ("s" if hours != 1 else "")
    return said + (f" {rest} minutes" if rest else "")


def _state_kind(state) -> str:
    """TorBox's `download_state` as one of the few things a viewer is told: finding (looking for peers), stalled,
    downloading, checking (its own data), paused, finishing (the copy to its storage), or working (anything else)."""
    state = str(state or "").lower()
    if state.startswith("metadl"):
        return "finding"
    if state.startswith("stalled"):
        return "stalled"
    if state.startswith("checking"):                 # checkingDL, checkingResumeData
        return "checking"
    if state.startswith("paused"):
        return "paused"
    if state.startswith("uploading"):
        return "finishing"
    if state == "downloading":
        return "downloading"
    return "working"


def _reading_kind(reading: dict | None) -> str:
    """`_state_kind` of one TorBox reading (`state`, `progress`, `downloaded`: its `total_downloaded`, `finished` and
    `present`: its `download_finished` and `download_present`). A finished download is TorBox's, whatever its state
    says: "have" once it is present too (TorBox has it, and the play opens it), else "finishing" (`processing`,
    measured 2026-10-23 with `download_finished` true and `download_present` false, and `completed` before the copy is
    present). A `checking`
    reading with nothing downloaded (progress 0 and no bytes) is TorBox still starting the download, not checking what
    it has: it has nothing to check. Seen live as `checking` for 95 s with `size` -1, `total_downloaded` 0, 0 seeds
    and 0 peers, and as a fresh add's `checkingResumeData` at 0 %. It reads as "finding" (TorBox is finding
    people who share it), which is what 0 seeds and 0 peers are and what the `metaDL` before it already said. Bytes
    unknown (a reading made without them) count as none; a reading with bytes keeps "checking"."""
    reading = reading or {}
    state = str(reading.get("state") or "").lower()
    if reading.get("finished") and reading.get("present"):
        return "have"
    if reading.get("finished") or state in ("processing", "completed"):
        return "finishing"
    kind = _state_kind(state)
    if kind != "checking" or int(reading.get("progress") or 0):
        return kind
    try:
        downloaded = float(reading.get("downloaded") or 0)
    except (TypeError, ValueError):                  # not a byte count: as unknown
        downloaded = 0.0
    return "checking" if downloaded > 0 else "finding"


# What TorBox is doing, as the wait's line (`wait_line`) and the held line (`held_line`) say it.
_STATE_WORDS = {
    "finding": "TorBox is finding people who share it",
    "stalled": "Nobody is sharing it right now; TorBox keeps trying",
    "checking": "TorBox is checking what it has downloaded",
    "paused": "TorBox has paused the download",
    "finishing": "TorBox is finishing the download",
    "have": "TorBox has it; opening it",
    "working": "TorBox is working on it",
}


# The same, after "Waiting until TorBox is done; ".
_UNTIL_DONE_WORDS = {
    "finding": "it is finding people who share it",
    "stalled": "nobody is sharing it right now",
    "checking": "it is checking what it has downloaded",
    "paused": "it has paused the download",
    "finishing": "it is finishing the download",
    "have": "it has it; opening it",
    "working": "it is working on it",
}


def speed_words(bytes_per_s) -> str | None:
    """A download speed as the line says it, in decimal units: "12 MB/s" from 10 MB/s, "1.8 MB/s" from 1 MB/s,
    "372 KB/s" from 1 KB/s; None below that or when unknown (TorBox has answered 5 B/s for a download that had just
    lost its one seed). The unit is chosen from the rounded value, as `eta_words` chooses its form, so 9.96 MB/s reads
    "10 MB/s", not "10.0 MB/s", and 999.6 KB/s "1.0 MB/s", not "1000 KB/s"."""
    try:
        bps = float(bytes_per_s)
    except (TypeError, ValueError):
        return None
    if bps < 1e3:
        return None
    kb = int(bps / 1e3 + 0.5)
    if kb < 1000:
        return f"{kb} KB/s"
    tenths = int(bps / 1e5 + 0.5)
    if tenths < 100:
        return f"{tenths // 10}.{tenths % 10} MB/s"
    return f"{int(bps / 1e6 + 0.5)} MB/s"


def _numbers(reading: dict, kind: str, speed: bool = True) -> str | None:
    """The numbers for one reading, as the lines say them: the percentage, and while TorBox is downloading the speed
    and the time left the wait worked out from TorBox's byte counts (`speed`, `left_s`: `providers.ByteRate`), each
    only when it has one ("43 %, 1.8 MB/s, about 3 minutes left"); "so far" when it is not downloading right now
    ("33 % so far", no speed and no time left). TorBox's own `eta` is never said: it swung from 1 hour 10 minutes to
    7 hours 50 minutes to 9 minutes within four minutes of one download (trace `ui-play-1792707646-8709`). None until
    TorBox reports progress (downloading, a percentage, or bytes downloaded), and none once TorBox has finished
    downloading ("finishing", "have"): its counters can stop at 32 % then (162 MB of 503 MB at `completed`, measured
    2026-10-23), and the state's words say all there is."""
    if kind in ("finishing", "have"):
        return None
    progress = int(reading.get("progress") or 0)
    try:
        downloaded = float(reading.get("downloaded") or 0)
    except (TypeError, ValueError):
        downloaded = 0.0
    if kind != "downloading" and progress <= 0 and downloaded <= 0:
        return None
    if kind != "downloading":
        return f"{progress} % so far"
    said = [f"{progress} %"]
    fast = speed_words(reading.get("speed")) if speed else None
    left = eta_words(reading.get("left_s"))
    said += [w for w in (fast, f"{left} left" if left else None) if w]
    return ", ".join(said)


def wait_line(reading: dict | None, until_done: bool = False) -> str:
    """What TorBox is doing with the download, from one poll's reading (`state`, `progress`, `downloaded`, `finished`,
    `present`, and the wait's own `speed` and `left_s`): the loading screen's line while the play waits, with the
    numbers from TorBox's first reported progress ("TorBox is downloading it: 43 %, 1.8 MB/s, about 3 minutes left").
    `until_done`: the viewer chose to keep waiting from a hold, and the line says so first. Once TorBox has the episode
    the line says so, whatever came before ("TorBox has it; opening it"). TorBox's state names never reach the
    viewer."""
    reading = reading or {}
    kind = _reading_kind(reading)
    numbers = _numbers(reading, kind)
    if kind == "have":
        return _STATE_WORDS[kind]
    if until_done:
        if kind == "downloading":
            return f"Waiting until TorBox is done: {numbers}"
        return "Waiting until TorBox is done; " + _UNTIL_DONE_WORDS[kind] + (f" ({numbers})" if numbers else "")
    if kind == "downloading":
        return f"TorBox is downloading it: {numbers}"
    return _STATE_WORDS[kind] + (f" ({numbers})" if numbers else "")


def original_language_words(original: str | None) -> str:
    """ "in Japanese", or "in its original language" when the show's country is not one the table names."""
    return f"in {original}" if original else "in its original language"


def wait_headline(show: str | None, episode, original: str | None, held: bool = False, reading: dict | None = None,
                  nothing_cached: bool = False, added_dub: str | None = None) -> str:
    """The choice's first sentence: TorBox has only a dub, and what it is doing with the original-audio copy. Held, the
    second sentence follows TorBox's last reading (a stalled download is not "still downloading"). `nothing_cached`:
    TorBox has no copy of the episode at all, so the sentence names no language (one would imply another it has).
    `added_dub`: the one download is itself a dub (`dubs=allow`, or every release a dub), named by its language ("" for
    an unknown one), so the viewer knows before "ready in English" what they are waiting for."""
    what = f"{show} episode {episode}" if show else f"episode {episode}"
    first = (f"TorBox doesn't have {what} yet." if nothing_cached
             else f"TorBox doesn't have {what} {original_language_words(original)} yet.")
    it = "it" if added_dub is None else (f"the {added_dub} dub" if added_dub else "the dub")
    if not held:
        return f"{first} It's downloading {it} now."
    # A hold never carries a reading TorBox has finished and holds (that reading is ready, and the wait ends); if one
    # ever did, it reads as finishing, not as "doesn't have … It has it now".
    kind = _reading_kind(reading)
    then = {"downloading": f"It's still downloading {it}.", "finding": f"It's still looking for people who share {it}.",
            "stalled": f"Nobody is sharing {it} right now.",
            "checking": "It's checking what it has so far." if added_dub is None
            else f"It's checking what it has of {it} so far.",
            "paused": "It has paused the download." if added_dub is None else f"It has paused {it}.",
            "finishing": "It's finishing the download." if added_dub is None else f"It's finishing {it}.",
            "working": f"It's still working on {it}."}["finishing" if kind == "have" else kind]
    return f"{first} {then}"


def held_line(reading: dict | None) -> str:
    """The held play's line: what TorBox was last doing, with the percentage once it reported progress and the time
    left when the wait had one from TorBox's bytes ("TorBox is still downloading it: 33 %, about 1 hour 20 minutes
    left"). No speed: the hold is the play's last word, and a speed there would stand still."""
    reading = reading or {}
    kind = _reading_kind(reading)
    numbers = _numbers(reading, kind, speed=False)
    if kind == "downloading":
        return f"TorBox is still downloading it: {numbers}"
    return _STATE_WORDS[kind] + (f" ({numbers})" if numbers else "")


def dub_choice_label(dub_lang: str | None, subtitles: str | None) -> str:
    """ "Watch the English dub now (with Arabic subtitles)"; the subtitle language only when a verified source for it
    exists for the dub's play."""
    what = f"the {dub_lang} dub" if dub_lang else "the dub"
    return f"Watch {what} now" + (f" (with {subtitles} subtitles)" if subtitles else "")


KEEP_WAITING = "Keep waiting"
TELL_ME = "Tell me when it's ready"


def languages_words(original: str | None, subtitles: str | None) -> str:
    """ "in Japanese with Arabic subtitles", "in Japanese", "with Arabic subtitles", or ""."""
    said = f"in {original}" if original else ""
    if subtitles:
        said = f"{said} with {subtitles} subtitles".strip()
    return said


def ready_body(episode, original: str | None, subtitles: str | None) -> str:
    """A notification's body for an episode TorBox has now: "Episode 3 is ready in Japanese with Arabic subtitles."
    (the subtitle language only when it was promised)."""
    langs = languages_words(original, subtitles)
    return f"Episode {episode} is ready" + (f" {langs}." if langs else ".")


# Why a pending download was given up, as its notification says it (`gave_up_body`).
GAVE_UP_WHY = {
    "nobody": "nobody is sharing it",
    "missing": "TorBox no longer has it",
    "refused": "TorBox didn't accept your account",
}


def gave_up_reason(why: str, reading: dict | None = None) -> str:
    """The reason a give-up is told with. By age, the reading the watcher just made decides: stalled or finding (a
    `checking` with nothing downloaded included), nobody shares it ("nobody"); downloading or any other state, it is
    still on its way ("slow"); no reading at all (the poll failed), a neutral "day"."""
    if why != "age":
        return why
    if (reading or {}).get("state") is None:
        return "day"
    return "nobody" if _reading_kind(reading) in ("stalled", "finding") else "slow"


# A give-up's next step when TorBox had no copy at all: another release (another group, another swarm) is the one
# thing that can still bring the episode. Browse's episode list opens the release list (`p`); a notification's click
# runs only the plugin's own command, which has no way to open it.
CHOOSE_ANOTHER = "You can choose another release for it in Browse."


def gave_up_body(episode, original: str | None, reason: str, nothing_cached: bool = False) -> str:
    """ "Episode 3 couldn't be downloaded in Japanese: nobody is sharing it."; for a download still on its way, or
    one not read, the watcher stopped waiting, which is what happened: "We stopped waiting for episode 3 in Japanese:
    TorBox was still downloading it after a day."

    `nothing_cached`: TorBox had no copy of the episode, so there is no dub to offer and no language to set apart. The
    body names none and ends with the one step left that can work (`CHOOSE_ANOTHER`), except for a refused account,
    which no release would fix."""
    where = f" in {original}" if original and not nothing_cached else ""
    if reason == "slow":
        said = f"We stopped waiting for episode {episode}{where}: TorBox was still downloading it after a day."
    elif reason not in GAVE_UP_WHY:
        said = f"We stopped waiting for episode {episode}{where} after a day."
    else:
        said = f"Episode {episode} couldn't be downloaded{where}: {GAVE_UP_WHY[reason]}."
    return f"{said} {CHOOSE_ANOTHER}" if nothing_cached and reason != "refused" else said


def dub_action_label(dub_lang: str | None) -> str:
    """The give-up notification's action: "Watch the English dub"."""
    return f"Watch the {dub_lang} dub" if dub_lang else "Watch the dub"


# What a click on a notification does, said at the end of its body: Omarchy's card draws no buttons.
CLICK_PLAY = "Click to play it."


def click_dub(dub_lang: str | None) -> str:
    """ "Click to watch the English dub." """
    return f"Click to watch the {dub_lang} dub." if dub_lang else "Click to watch the dub."


def ready_paused(episode) -> str:
    """The player's line for an episode that loaded paused because nobody was watching when it was ready."""
    return f"Episode {episode} is ready. Space plays it."


def player_closed(episode, ready: bool = False) -> dict:
    """The player went away while a play was starting it (`mpv.PlayerClosed`): what happened, and the one thing to do.
    Not a bug to report: a player restart, or the window closed, does it. `ready`: it went during the wait for TorBox,
    which has the episode now (the viewer was sent the ready notification)."""
    if ready:
        return {"headline": f"The player closed while TorBox was downloading episode {episode}.",
                "action": "It's ready now: play it again.", "hint": ""}
    return {"headline": f"The player closed while starting episode {episode}.", "action": "Play it again.",
            "hint": ""}


def certificate_refused(episode, provider=None) -> dict:
    """The player refused the stream server's certificate (`--tls-verify=yes`): it could not confirm the server was the
    real one, so it sent nothing and opened nothing. Almost always the network in between, not the service."""
    name = NEXT_NAMES.get(str(provider or ""))
    server = f"{name}'s server" if name else "the video server"
    return {"headline": f"The player couldn't confirm {server} was genuine, so it didn't open episode {episode}.",
            "action": "Try again on another network: café, hotel and work networks can get in the way.", "hint": ""}


def ready_in_the_background(episode) -> str:
    """The popout's line for an episode that became ready while the viewer watched another one: nothing was
    switched; the notification, Browse or the player's own episode list plays it (any play of it finds it ready)."""
    return f"Episode {episode} is ready. Play it from the notification, from Browse or from the player's episode list."


def later_line(episode) -> str:
    """What the viewer is told once "Tell me when it's ready" is recorded, on the surface they pressed it on and in the
    popout: the acknowledgement, and the ended play's own line."""
    return f"We'll tell you when episode {episode} is ready." if episode else "We'll tell you when it's ready."


def wait_stopped(progress) -> dict:
    """The play was stopped while it waited for TorBox's download; the download carries on there."""
    return {"headline": f"Stopped while TorBox was downloading it ({int(progress or 0)} %).",
            "action": QUEUED_ACTION, "hint": ""}
# TorBox had no download server free (NO_SERVERS_AVAILABLE_ERROR). Its side, and temporary.
UNAVAILABLE_HEADLINE = "TorBox can't start downloads right now."
UNAVAILABLE_ACTION = "Try again later."
# TorBox answered DATABASE_ERROR or DOWNLOAD_SERVER_ERROR (or AUTH_ERROR). Also its side; its docs say to
# wait a while.
SERVER_SIDE_HEADLINE = "TorBox ran into an error on its side."
SERVER_SIDE_ACTION = "Try again in a little while."
# {kind: (headline, action, the pinned pack's status line while searching past it)}; any kind not listed
# (a server-side one: "database", "download_server", or "auth") takes the "server_side" row.
_TORBOX_SIDE = {
    "no_servers": (UNAVAILABLE_HEADLINE, UNAVAILABLE_ACTION,
                   "TorBox can't start your pinned pack right now, so looking for this episode elsewhere"),
    "server_side": (SERVER_SIDE_HEADLINE, SERVER_SIDE_ACTION,
                    "TorBox ran into an error with your pinned pack, so looking for this episode elsewhere"),
}


def probe_kind(probe: dict) -> str:
    """What one provider answer (a `probe_errors` row) means, read from its fields and
    never from its words (the word "uncached" in an error used to decide it).

    "queued"      — the provider holds the release and is downloading it (`queued`, with `progress`);
    "skipped"     — not sent to the provider at all (`skipped`: why, e.g. "seeders");
    "unavailable" — the provider cannot take it right now, on its own side (`unavailable`: "no_servers",
                    "database", "download_server" or "auth");
    "blocked"     — the provider refused the account (the row of the guard that stops asking it again);
    "dubbed"      — the provider has it, but its audio read as a dub and `dubs=avoid` set it aside (`dubbed`: the
                    dub's language, or "");
    "other"       — any other answer.
    """
    if probe.get("queued"):
        return "queued"
    if probe.get("skipped"):
        return "skipped"
    if probe.get("unavailable"):
        return "unavailable"
    if probe.get("stage") == "provider.block":
        return "blocked"
    if probe.get("dubbed") is not None:
        return "dubbed"
    return "other"


def torbox_downloading(probes: list[dict]) -> int | None:
    """The progress (%) of the first release TorBox queued, when one did; else None."""
    for probe in probes or []:
        if probe_kind(probe) == "queued" and probe.get("provider") == "torbox":
            return int(probe.get("progress") or 0)
    return None


def torbox_unavailable(probes: list[dict]) -> bool:
    """Whether TorBox refused for a reason on its own side (any `unavailable` kind)."""
    return any(probe_kind(p) == "unavailable" and p.get("provider") == "torbox" for p in probes or [])


def torbox_side_kind_words(kind: str) -> tuple[str, str, str]:
    """(headline, action, pinned-pack status line) for one kind of error on TorBox's side (a probe row's
    `unavailable`): "no_servers", or a server-side kind ("database", "download_server", "auth")."""
    return _TORBOX_SIDE["no_servers" if kind == "no_servers" else "server_side"]


def torbox_side_words(probes: list[dict]) -> tuple[str, str] | None:
    """(headline, action) when TorBox's own side is why the play stopped, else None. One place, so the
    panel (`failure`) and the popout and overlay (`cli.diagnose_failure`) cannot drift apart.

    No server free wins: it is also why every later uncached add was not sent. A
    database or download-server error is about one download and later adds were still sent, so it
    is named only when no TorBox add that was sent got another answer ("other"): that answer is about its
    release, and "wait and retry" would hide that the other releases are bad."""
    torbox = [p for p in probes or [] if p.get("provider") == "torbox"]
    kinds = {p.get("unavailable") for p in torbox if probe_kind(p) == "unavailable"}
    if not kinds:
        return None
    if "no_servers" in kinds:
        return torbox_side_kind_words("no_servers")[:2]
    if any(probe_kind(p) == "other" for p in torbox):
        return None
    return torbox_side_kind_words("server_side")[:2]


def dubbed_words(probes: list[dict]) -> dict | None:
    """A release TorBox has whose audio turned out to be a dub (`dubs=avoid` set it aside, a "dubbed" row), as the
    failure says it when nothing else played: what happened, and the two ways on. The one download of this play
    ("TorBox downloaded it, but …") is named first. None without such a row."""
    rows = [p for p in probes or [] if probe_kind(p) == "dubbed"]
    if not rows:
        return None
    row = next((p for p in rows if p.get("downloaded")), rows[0])
    lang = str(row.get("dubbed") or "").strip()
    what = f"the {lang} dub" if lang else "a dub"
    headline = (f"TorBox downloaded it, but it turned out to be {what}." if row.get("downloaded")
                else f"The copy TorBox has turned out to be {what}.")
    # A dub TorBox had cached all along (`dub_cached`, the held dub) is named as a pick in Browse: a pick plays whatever
    # its audio is, while allowing dubs plays whichever cached copy ranks first (the downloaded one, often), so the
    # setting is never said to play that dub.
    named = cached_dub_named(probes)
    action = (f"Choose another release in Browse (TorBox has {named}), or allow dubs in Settings to watch it dubbed."
              if named else DUBBED_ACTION)
    return {"headline": headline, "action": action, "hint": ""}


def cached_dub_named(probes: list[dict]) -> str | None:
    """The cached dub a TorBox row carries (`dub_cached`), as the dubbed words name it: "the English dub, Wrenfold
    1080p"; None without one."""
    dub = next((p.get("dub_cached") for p in probes or [] if p.get("provider") == "torbox" and p.get("dub_cached")),
               None)
    if not dub:
        return None
    lang = str(dub.get("lang") or "").strip()
    group = "".join(c for c in str(dub.get("group") or "") if c.isalnum() or c in " -")[:DUB_GROUP_CHARS].strip()
    res = dub.get("resolution")
    which = " ".join(w for w in (group, f"{res}p" if res else "") if w)
    what = f"the {lang} dub" if lang else "a dub"
    return f"{what}, {which}" if which else what


# What a viewer can do about a copy that turned out to be a dub: another release (another group) may carry the original
# audio; or, with dubs allowed, the next play plays a dub TorBox has (which one is the ranking's, so none is promised).
DUBBED_ACTION = "Choose another release in Browse, or allow dubs in Settings to watch it dubbed."


def failure(probes: list[dict], settings: dict, outlook: dict) -> dict:
    """Why a play did not happen, as a headline, an action, and an optional hint.

    `probes` and `outlook` are the same structures the log records; nothing here reaches into
    their stage names. The caller keeps them for the log.
    """
    if not list(probes or []):
        return {"headline": "Nothing was found for this episode.",
                "action": "", "hint": "It may not have been released yet."}
    downloading = torbox_downloading(probes)
    if downloading is not None:
        # A release TorBox queued is why the play stopped. First, because it is the
        # one thing that fixes itself: a blocked second provider does not change what to do.
        return {"headline": f"TorBox is downloading it ({downloading} %).",
                "action": dub_clause(probes),
                "hint": "Press p to pick another release."}
    dubbed = dubbed_words(probes)
    side = torbox_side_words(probes)
    if side and dubbed is not None and not any(probe_kind(p) == "queued" for p in probes):
        # TorBox's own side refused the add: that stays the headline (another release fails the same way this hour),
        # and the copy that turned out to be a dub is the hint, in the dubbed words (a cached dub named as a pick).
        return {"headline": side[0], "action": side[1], "hint": f"{dubbed['headline']} {dubbed['action']}"}
    if dubbed is not None:
        return dubbed
    if side and not any(probe_kind(p) == "queued" for p in probes):
        # TorBox's own side refused, so the viewer is told that, never that the pack or the
        # account is at fault. Ahead of a blocked provider: a stale Real-Debrid verdict can add a
        # blocked row even to a play that never asked Real-Debrid. Not when another provider queued a
        # release: "TorBox can't start downloads" would hide a download carrying on.
        return {"headline": side[0], "action": dub_clause(probes, side[1]), "hint": ""}
    return {
        "headline": "This episode could not be opened.",
        "action": dub_clause(probes, "Try again in a little while."),
        "hint": "",
    }


# Every reason `score.prefilter_reason` can give, as a short clause (the surfaces cap a line at 120).
_DROPPED = {
    "size": "the wrong size",
    "no seeders": "with nobody sharing",
    "pack range": "without this episode",
    "batch": "whole-season, turned off",
}
_DROPPED_ADVICE = {
    "size": "No full-size copy of this episode exists yet; there is nothing to play for now.",
    "no seeders": "Nobody is sharing any copy right now; try again later, or choose one yourself.",
    "pack range": "Try again later; a copy of this episode on its own may appear.",
    "batch": "Turn on whole-season collections (allowBatches) in settings to use them.",
}


# Why the release picker shows a release dimmed, one short phrase per rejection code.
_REJECTED = {
    "series": "names another show",
    "season": "another season",
    "batch": "whole-season, turned off",
    "pack_range": "this episode is outside its range",
    "seeders": "nobody sharing",
    "size": "the wrong size",
    "pack_files": "this episode is not among its files",
    "other_show": "another show",
    "episode": "another episode",
}


def rejected_why(code: str, detail: str | None = None) -> str:
    """The viewer's words for one rejected release; `detail` names the other show."""
    words = _REJECTED.get(code, "not usable")
    if code == "other_show" and detail:
        return f"{words}: {detail.title()}"
    if code == "episode" and detail:
        return f"the title does not name episode {detail}"
    return words


def filtered_out(dropped: dict, total: int) -> dict:
    """Every copy found was dropped before ranking: why, in the viewer's words, and what to do."""
    ranked = sorted(dropped.items(), key=lambda kv: -kv[1])
    parts = [f"{n} {_DROPPED.get(reason, 'unusable')}" for reason, n in ranked[:2]]
    rest = sum(n for _, n in ranked[2:])
    if rest:
        parts.append(f"{rest} other")
    noun = "copy" if total == 1 else "copies"
    top = ranked[0][0] if ranked else ""
    return {"message": f"Found {total} {noun} of this episode, none playable: {'; '.join(parts)}.",
            "suggest": _DROPPED_ADVICE.get(top, "Try again later.")}


# ----------------------------------------------------------------------------- Torznab servers
# The Accounts sheet's words for a Torznab server: what a check found, what is wrong and what to do. Here, not in
# the add-on or the sheet, so the copy rules above (FORBIDDEN, JARGON, whole sentences) hold for them too.

TORZNAB_EMPTY = ("No Torznab server. Add your Prowlarr or Jackett with its address and API key; "
                 "the plugin then searches it.")
TORZNAB_GONE = "That server is no longer in the file; the list has been read again."
TRY_AGAIN = "Try again in a moment."
# What to do about an indexer Prowlarr has paused, and about several (`paused_where`).
PAUSED_WHERE = ("Prowlarr's System > Status lists it and its log says why; it is searched again when the pause "
                "ends.")
PAUSED_WHERE_MANY = ("Prowlarr's System > Status lists them and its log says why; they are searched again when the "
                     "pause ends.")
# Where to look for an indexer Prowlarr answered 500 for. Its error pipeline logs that exception at Fatal, and
# its log store keeps Info and above, which System > Events shows.
FAILED_WHERE = "Prowlarr's own log says why: System > Events in Prowlarr."
# Where each product shows its API key. The check's failure (`KEY_WHERE`) and the offer for a server found on this
# computer (`torznab_found`) are both built from these, so they cannot drift apart.
KEY_PLACE = {"Prowlarr": "under Settings > General", "Jackett": "at the top right of its dashboard"}
KEY_WHERE = (f"Check the API key: Prowlarr shows it {KEY_PLACE['Prowlarr']}, "
             f"Jackett {KEY_PLACE['Jackett']}.")

# {failure kind: (what is wrong, what to do)}; a passing failure (`transient`) is told to try again instead, and a
# saved add to check again, except a kind in `_KEEPS_ACTION`.
_TORZNAB = {
    "wrong_key": ("wrong API key", KEY_WHERE),
    "login": ("the server asks for a login",
              "A proxy or login page stands in front of it; give the address the server itself answers on."),
    "error": ("the server answered with an error", "Its own log says why; try again later."),
    "rate_limited": ("the server asked for a pause", TRY_AGAIN),
    "indexer_paused": ("paused by Prowlarr after its searches failed", PAUSED_WHERE),
    "indexer_failed": ("Prowlarr could not search it", FAILED_WHERE),
    "http_status": ("the server answered with an error", "Its own log says why; try again later."),
    "unreachable": ("the server did not answer", TRY_AGAIN),
    "timeout": ("no answer in time", TRY_AGAIN),
    "not_started": ("not asked in time", TRY_AGAIN),
    "too_large": ("its answer was too large", "Lower its results limit."),
    "redirect": ("it sends the plugin to another address",
                 "Give the address it moves to instead, for example its https:// address."),
    "unsafe": ("its answer is not a plain Torznab answer", "Check the address."),
    "not_xml": ("not a Torznab feed", "For Jackett give its Torznab feed address; for Prowlarr its own address."),
    "not_torznab": ("not a Torznab feed", "For Jackett give its Torznab feed address; for Prowlarr its own address."),
    "no_search": ("search is turned off on it", "Turn search on for it."),
    "no_category": ("no anime category", "Give a feed that offers TV/Anime (5070)."),
    "not_address": ("not a Torznab or Prowlarr address",
                    "Give Prowlarr's own address as the browser shows it (for example http://host:9696), or a "
                    "Jackett or other Torznab feed address."),
    "no_indexer": ("no indexer in it offers anime or movies",
                   "In Prowlarr, turn on an indexer that offers TV/Anime (5070) or Movies/Other (2020)."),
    "bad_list": ("Prowlarr's indexer list could not be read", "Check that the address is Prowlarr's own."),
}
# Kinds whose own action stands even for a passing failure and a saved add: Prowlarr's pause of a failing
# indexer grows up to a day (its EscalationBackOff), so "in a moment" would mislead; an indexer Prowlarr could not
# search is told where Prowlarr says why.
_KEEPS_ACTION = ("indexer_paused", "indexer_failed")
# A server's headline for a kind whose line words name one indexer, when every failing indexer failed so.
_HEADLINE = {"indexer_failed": "Prowlarr could not search any of its indexers"}
# The two kinds Prowlarr itself tells about, each with its own place to look.
_PROWLARR_SIDE = ("indexer_failed", "indexer_paused")
# Why a Prowlarr indexer is left out, by the add-on's reason code.
_LEFT_OUT = {"disabled": "turned off", "usenet": "usenet", "not_torrent": "of another kind",
             "no_category": "without anime or movies", "not_an_indexer": "not an indexer", "over_cap": "past the cap"}


def sentence(text: str) -> str:
    """One sentence: a capital first letter, one full stop at the end."""
    text = str(text or "").strip()
    return (text[:1].upper() + text[1:]).rstrip(".") + "." if text else ""


def torznab_problem(kind: str, message: str = "") -> str:
    """The viewer's words for a failure kind (a feed's or a server's); `message` for a kind this table lacks
    (e.g. why an address is refused, which is already a sentence)."""
    return _TORZNAB.get(str(kind or ""), (str(message or kind or "the check failed"), ""))[0]


def paused_where(n: int) -> str:
    """What to do about `n` indexers Prowlarr has paused."""
    return PAUSED_WHERE_MANY if n > 1 else PAUSED_WHERE


def _prowlarr_where(failed: int, paused: int) -> str:
    """Where Prowlarr says why, for `failed` indexers it could not search and `paused` ones it paused:
    its log (System > Events) and its System > Status, both when both kinds failed."""
    return " ".join(([FAILED_WHERE] if failed else []) + ([paused_where(paused)] if paused else []))


def _mixed_headline(failed: int, paused: int) -> str:
    """Every indexer failing, some inside Prowlarr and the others paused by it."""
    return (f"Prowlarr could not search {failed} of its indexers and paused {paused} after "
            f"{'its' if paused == 1 else 'their'} searches failed")


def torznab_words(result: dict, saved: bool | None = None) -> dict:
    """A check's answer as the sheet shows it: `headline` (what the server is, or what is wrong), `action` (what
    to do, "" when nothing), `lines` (each feed that cannot be searched, and a Prowlarr's indexers left out).
    `saved`: for an add, whether the server was saved. Indexer names are the server's own text; the sink scrubs
    them and the sheet draws them as plain text."""
    feeds = result.get("feeds") or []
    failing = [f for f in feeds if not f.get("ok")]
    failed, paused = (sum(f.get("problem") == k for f in failing) for k in _PROWLARR_SIDE)
    lines = [f"{f.get('indexer') or f.get('label')}: {torznab_problem(f.get('problem'), f.get('message'))}"
             for f in failing]
    dropped = {k: v for k, v in (result.get("dropped") or {}).items() if v}
    if dropped:
        n = sum(dropped.values())
        lines.append(f"{n} {'indexer' if n == 1 else 'indexers'} in Prowlarr left out: "
                     + ", ".join(f"{v} {_LEFT_OUT.get(k, 'for another reason')}" for k, v in dropped.items()))
    if result.get("ok"):
        if result.get("kind") == "prowlarr":
            n = len(feeds)
            what = f"Prowlarr with {n} {'indexer' if n == 1 else 'indexers'}"
            if failing:
                what += f"; {len(failing)} cannot be searched now"
        else:
            what = "a Torznab feed" if len(feeds) <= 1 else f"{len(feeds)} Torznab feeds"
        # The server passed, but an indexer Prowlarr could not search is told where Prowlarr says why, and a
        # paused one beside it where that is; paused ones alone keep "" here
        action = _prowlarr_where(failed, paused) if failed else ""
        return {"headline": sentence(("Saved: " + what) if saved else what), "action": action, "lines": lines}
    problem = str(result.get("problem") or "")
    kinds = {f.get("problem") for f in failing}
    if failing and kinds == set(_PROWLARR_SIDE):
        said = _mixed_headline(failed, paused)
    elif failing and kinds == {problem} and problem in _HEADLINE:
        said = _HEADLINE[problem]
    elif problem in _HEADLINE:                  # its line words name one indexer; other kinds failed beside it
        said = "none of its indexers can be searched now"
    else:
        said = torznab_problem(problem, result.get("message"))
    keep = problem in _KEEPS_ACTION
    action = TRY_AGAIN if result.get("transient") and not keep else _TORZNAB.get(problem, ("", ""))[1]
    if problem in _PROWLARR_SIDE:
        action = _prowlarr_where(failed, paused) or action
    if saved is True:
        headline = f"Saved, but the check did not finish: {said}"
        action = action if keep else "Check it again in a moment."
    elif saved is False:
        headline = f"Not saved: {said}"
    else:
        headline = said
    return {"headline": sentence(headline), "action": action, "lines": lines}


def torznab_found(product: str, hostport: str) -> dict:
    """The offer for a Prowlarr or Jackett found running on this computer: the product and its address, the
    sheet's button to press (its label is "Add <product>"), and where that product shows its API key. Adding it is
    the normal add and check."""
    place = KEY_PLACE.get(product)
    return {"headline": f"Found {product} on this computer ({hostport}). Press Add {product}, then paste its API key.",
            "action": f"Its API key is {place}." if place else KEY_WHERE}


def torznab_caption(problem: str, skipped: list, path: str, servers: int) -> str:
    """The line above the sheet's Torznab rows: why the file cannot be used or changed, or how to add a server."""
    if problem:
        return sentence("The servers file cannot be used: " + str(problem))
    if skipped:
        first = skipped[0]
        return (f"Server {first.get('server')} in {shown_path(path)} cannot be read: "
                f"{sentence(first.get('reason'))} Fix it there before adding or removing a server here.")
    return "" if servers else TORZNAB_EMPTY
