"""`omarchy-anime status`: the bar's state, without importing the CLI.

`Service.qml` runs `status` every ten seconds for as long as the shell is up, playing or not, and every run is a new
Python process. Through `cli.main` each one imported the whole CLI (the scorer, the indexers, `concurrent.futures`,
the add-on loaders), about 86 ms of the 193 ms of CPU an idle call cost, to read a socket and a few small files. So
`bin/omarchy-anime` hands exactly `status`, `--trace T status` and `--trace=T status` to `main` here, and everything
else to `cli.main`.

Only what `player_state` reads is imported. `cli` re-exports `player_state`, `SHEET_SETTINGS`, `ADVANCED_SETTINGS`
and `emit_json` from here, so `cli.main(["status"])` and `cli.player_state` keep working, and both entry points
print one function's output. `main` and `cli.main` share the clamp notes and the two failure branches
(`report_setting_notes`, `fail_stage`, `fail_internal`), so the trace's `cli start`/`end` and `fail` lines are the
same from either; a test compares both entry points' stdout and log lines.

The two directories and the socket are read through their modules at call time (`core.CACHE_DIR`,
`core.CONFIG_DIR`, `mpvmod.MPV_SOCKET`), not copied at import: the tests point those at temp folders, and a copy
here would read the user's real ones. In a real run they are the same values `cli` holds.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time

from . import core
from . import covers
from . import mpv as mpvmod
from . import player as playermod
from . import playstate
# The log scrubber's first line imports this for the Torznab keys (`core.known_secrets`). Imported
# here, before `main` starts its clock, so `cli end`'s `ms` measures the command, as it did under `cli.main`.
from . import torznab_servers  # noqa: F401
from .anilist import AniList, RATE_FILE, rate_record, token_expiry
from .core import (LANGUAGE_NAMES, LAST_SETTING_NOTES, VERSION, Interrupted, Log, StageError, cached_json,
                   load_settings, load_settings_with_sources, redact_text, safe_argv, shell_widget_settings)
from .mpv import Mpv

SHEET_SETTINGS = ("subtitleLanguage", "subtitleFallbackLanguage", "subtitlePolicy", "dubs", "preferredResolution",
                  "subtitleFont",
                  "watchedThreshold", "subtitleSyncMaxOffset", "pollIntervalSec", "maxTitleWidth",
                  "autoNext", "prefetchNext", "showTitleInBar", "showCompletedInTop", "allowBatches",
                  "sideloadSubtitles")
# Behind the sheet's **Advanced** disclosure, and editable there — hosts, lists and
# budgets, not knobs turned during an episode. Before that these were `READ_ONLY_SETTINGS`: the
# sheet printed them and told the user to go and create a JSON file by hand, which answers
# "where is that set?" and then refuses to help.
#
# Together the two tuples cover **every** key in `core.DEFAULT_SETTINGS`; a test
# pins that, which is what would have caught `anilistClientId` falling through both.
ADVANCED_SETTINGS = ("indexers", "indexerTimeoutSeconds", "preferredGroups", "englishOnlyGroups", "providers",
                     "probeCount", "providerBlockMinutes", "rdPollSeconds", "torboxWaitSeconds",
                     "torrentInfoSeconds",
                     "subtitleCheckCount", "subtitleCheckSeconds", "subsCacheMaxBytes",
                     "httpTimeout", "anilistClientId")


def player_state(log: Log, mpv: Mpv, running: bool) -> dict:
    """One JSON object for the service and the bar: mpv properties, the Hyprland window, now-playing, resume."""
    out: dict = {"running": running, "responsive": running and mpv.healthy(timeout=3.0), "window": None,
                 "parked": False, "visible": False}
    out["socketPath"] = str(mpvmod.MPV_SOCKET)       # the shell connects here when it has no XDG_RUNTIME_DIR of its own
    if running and not out["responsive"]:
        log.warn("mpv.watchdog", "status", result="FAIL",
                 suggest="`omarchy-anime player start` replaces the hung player")
        out["pid"] = mpv._owner_pid()
    if out["responsive"]:
        for prop in ("pid", "pause", "time-pos", "duration", "percent-pos", "media-title", "idle-active", "eof-reached",
                     "paused-for-cache", "current-tracks/sub/lang", "current-tracks/audio/lang", "sub-delay"):
            try:
                out[prop] = mpv.command("get_property", prop)
            except StageError:
                out[prop] = None
        w = playermod.find_window(log, out.get("pid"))
        out["window"] = playermod.window_summary(w)
        out["parked"] = bool(out["window"] and out["window"]["parked"])
        # On screen, parked or not; one `monitors` read, and only when there is a window.
        out["visible"] = playermod.window_seen(log, w)
    # mpv reads `--script=` once, at process start, so a player older than the Lua script
    # is running the previous copy of it while every other signal here says it is healthy. Four
    # `stat` calls and two small `/proc` reads, no IPC — cheap enough for the 10 s poll. It is not
    # logged from here on purpose: a warn line every ten seconds is noise, so the service says it
    # once when it first sees it, the way the socket notice does.
    out["stale"] = playermod.staleness(out.get("pid"))
    # The pin-flow token lasts a year and cannot be refreshed. When it expires every read still
    # works (reads need no token) and only writes fail, which reads as "my progress stopped saving"
    # — so the warning has to come from here, where the bar already asks every ten seconds.
    try:
        out["anilist"] = token_expiry(AniList.load_token())
    except StageError as exc:
        out["anilist"] = {"expiresAt": None, "daysLeft": None, "expiring": False, "expired": False,
                          "error": str(exc)}
    # The UI warns at 90 % of the AniList budget. The counters come from the last
    # response any CLI process saw (anilist-rate.json); an expired window says nothing, and nor does a record out of
    # AniList's range (`rate_record`), silently: a line every ten seconds would be noise. Such a record was
    # written before the check; each CLI command's client ignores it too and says so (`anilist rate_record IGNORED`),
    # and the next answer rewrites it.
    rate = rate_record(cached_json(core.CACHE_DIR / RATE_FILE, None))
    if rate:
        limit, remaining = rate["limit"], rate["remaining"]
        out["anilist"]["rate"] = {"remaining": remaining, "limit": limit,
                                  "resetIn": int(rate["reset"] - time.time()) + 1,
                                  "warn": remaining <= max(1, round(limit * 0.1))}
    # The in-flight play, whoever started it. `Service.qml` used to know only about plays
    # it had spawned itself, so a play started from the in-player panel left `busy`/`lastError`
    # empty for its whole 28-43 s and its failure was never shown anywhere.
    out["play"] = playstate.public()
    np = playermod.read_now_playing()
    # Recomputed on every poll rather than trusted from the file: a warm that finished after the
    # play started must reach the popout, and a cover deleted by hand must stop being offered.
    out["nowPlaying"] = covers.attach_now_playing(np) if np else np
    out["resume"] = playermod.read_resume(np["mediaId"], np["episode"]) if np else None
    # The merged settings (defaults < config.json < shell.json) so the QML side never re-reads config itself.
    # Every key the settings sheet can show, plus where each effective value came from — the sheet
    # captions an overridden key with both, so the precedence is visible instead of mysterious.
    s, src = load_settings_with_sources()
    out["settings"] = {k: s.get(k) for k in SHEET_SETTINGS}
    out["settingsSource"] = {k: src.get(k, "default") for k in SHEET_SETTINGS}
    out["settingsAdvanced"] = {k: s.get(k) for k in ADVANCED_SETTINGS}
    out["settings"].update({k: s.get(k) for k in ADVANCED_SETTINGS})
    # A language setting shows the variant the viewer chose (`pt-BR`), the value its menu offers; the CLI reads the
    # table code and `<key>Variant`.
    for key in ("subtitleLanguage", "subtitleFallbackLanguage"):
        if key in out["settings"]:
            out["settings"][key] = core.setting_variant(s, key) or out["settings"][key]
    out["settingsSource"].update({k: src.get(k, "default") for k in ADVANCED_SETTINGS})
    # Kept under its old name for one release: an installed Service.qml that has not been
    # restarted yet still reads `settingsReadOnly` (the service is not hot-reloaded).
    out["settingsReadOnly"] = dict(out["settingsAdvanced"])
    # The **raw** shell.json entry, not the merge. `shell.updateEntryInline` replaces the entry
    # wholesale, so the sheet has to copy every key the entry already carries forward — and writing
    # the merged values instead would materialise all thirteen keys into shell.json and silently
    # shadow the user's config.json for every one of them. Only a key the user actually changed
    # belongs in here.
    out["settingsEntry"] = shell_widget_settings()
    # The language names the sheet labels its language choices with (one table, `core.LANGUAGE_NAMES`).
    out["languageNames"] = {**{code: names[0] for code, names in LANGUAGE_NAMES.items()},
                            **{variant: name for variant, (_, name, _) in core.LANGUAGE_VARIANTS.items()}}
    out["configPath"] = str(core.CONFIG_DIR / "config.json")
    return out


def emit_json(out) -> None:
    """One JSON result on stdout, scrubbed.

    `status` carries whatever mpv reports for `media-title`, which for a TorBox play is the CDN URL
    and therefore the account API key. Every stdout path goes through here so the QML side, the
    terminal and a shell pipeline all get the same scrubbed bytes.
    """
    sys.stdout.write(core.json_controls(redact_text(json.dumps(out, ensure_ascii=False, indent=2))) + "\n")


def do_status(log: Log) -> dict:
    """`cli.do_status` for the lean entry: the socket is the one `Mpv` connects to."""
    mpv = Mpv(log)
    try:
        running, foreign = mpvmod.MPV_SOCKET.exists() and mpv._try_connect(), None
    except mpvmod.ForeignPeer as exc:
        # Another user answers on the socket path (`mpv.peer refused` says who): no player of ours, and the rest of the
        # poll's answer still goes out, rather than the whole poll failing every 10 s.
        running, foreign = False, str(exc)
    out = player_state(log, mpv, running)
    return {**out, "playerWarning": foreign} if foreign else out


def report_setting_notes(log: Log, config_dir) -> None:
    """A value outside its bounds degrades to the default, loudly. config.json is hand-edited and `omarchy
    bar set` takes any string, so the control cannot be the guard. Read after `load_settings()`."""
    for note in LAST_SETTING_NOTES:
        log.warn("config.bounds", "clamp", result="FAIL", key=note["key"], given=note["given"],
                 bounds=note["bounds"], using=note["using"],
                 suggest=(note.get("suggest") or f"{note['key']} is out of range; using the default")
                         + f". Fix it in the settings sheet or in {core.home_shown(str(config_dir / 'config.json'))}")


def fail_stage(log: Log, exc: StageError, t0: float, trace: str) -> int:
    """A stage that failed: one `fail` line, one JSON result, exit 1 (130 for Ctrl-C)."""
    log.error(exc.stage, "fail", result="FAIL", ms=(time.monotonic() - t0) * 1000, error=str(exc),
              suggest=exc.suggest, **exc.data)
    emit_json({"ok": False, "trace": trace, "stage": exc.stage, "error": str(exc), "suggest": exc.suggest,
               **{k: v for k, v in exc.data.items() if k != "body"}})
    return 130 if isinstance(exc, Interrupted) else 1


def fail_internal(log: Log, exc: Exception, t0: float, trace: str) -> int:
    """The last resort: the popout and the trace must still see a reason."""
    import traceback
    tb = traceback.format_exc()
    log.error("internal", "fail", result="FAIL", ms=(time.monotonic() - t0) * 1000,
              error=f"{type(exc).__name__}: {exc}", traceback=tb[-1500:],
              suggest="bug: report with `omarchy-anime trace " + trace + "`")
    sys.stderr.write(redact_text(tb))       # an exception's text can quote a key, as the log line above does
    emit_json({"ok": False, "trace": trace, "stage": "internal", "error": f"{type(exc).__name__}: {exc}",
               "suggest": "unexpected error; see the trace"})
    return 1


def lean_trace(argv: list[str]) -> str | None:
    """The `--trace` value when `argv` is one of the three shapes this module answers (`status`, `--trace T status`,
    `--trace=T status`), `""` for the first; None for anything else, which `cli.main`'s parser handles, `-h` and
    an abbreviated `--tr` included. A `T` starting with `-` is left to argparse too, which refuses it."""
    if argv == ["status"]:
        return ""
    if len(argv) == 3 and argv[0] == "--trace" and argv[2] == "status" and not argv[1].startswith("-"):
        return argv[1]
    if len(argv) == 2 and argv[0].startswith("--trace=") and argv[1] == "status":
        return argv[0][len("--trace="):]
    return None


def main(argv: list[str]) -> int:
    """`cli.main` for `status` alone: the same trace id, `cli start`/`end` lines, clamp notes, output and exits."""
    given = lean_trace(argv)
    if given is None:
        raise ValueError(f"status.main answers only `status` and `--trace T status`, not {argv!r}")
    trace = given or os.environ.get("OMARCHY_ANIME_TRACE") or f"status-{int(time.time())}-{random.randrange(16**4):04x}"
    os.environ["OMARCHY_ANIME_TRACE"] = trace
    log = Log(trace)
    t0 = time.monotonic()
    log.info("cli", "start", cmd="status", argv=safe_argv(argv), version=VERSION)
    try:
        load_settings()
        report_setting_notes(log, core.CONFIG_DIR)
        out = do_status(log)
        # The re-arm probe for the watcher of pending downloads: a file read and a lock try, and a start only when an
        # entry is pending and no watcher runs (`watcher.ensure`). It never fails the bar's answer.
        try:
            from . import watcher
            watcher.ensure(log, "the status poll found a pending download and no watcher")
        except Exception as exc:                 # noqa: BLE001 - the status answer matters more
            log.warn("watch", "ensure", result="FAIL", error=f"{type(exc).__name__}: {exc}")
        log.info("cli", "end", result="PASS", ms=(time.monotonic() - t0) * 1000)
        emit_json(out)
        return 0
    except StageError as exc:
        return fail_stage(log, exc, t0, trace)
    except Exception as exc:  # noqa: BLE001 - last resort: the popout and the trace must still see a reason
        return fail_internal(log, exc, t0, trace)
