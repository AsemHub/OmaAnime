"""Lifecycle tracing: read the JSONL log and render one trace id as an ordered timeline.

The contract: a trace id is minted once at the user action
and carried by every hop (`--trace`, `OMARCHY_ANIME_TRACE`, JSON results). All hops append to the
same daily JSONL file, so one `render()` shows click → mapping → index → score → provider → mpv.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import CACHE_DIR, escape_controls, redactor

LOG_DIR = CACHE_DIR / "log"
LAST_DAYS = 2           # the files `read_log_lines` reads by default: today's and yesterday's


def _parse_ts(value: str) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def read_log_lines(days: int = LAST_DAYS) -> list[dict]:
    """Today's and yesterday's files (a trace never spans more than a session)."""
    out: list[dict] = []
    scrub = redactor()
    today = datetime.now(timezone.utc).date()
    for d in range(days - 1, -1, -1):
        path = LOG_DIR / f"{today - timedelta(days=d):%Y-%m-%d}.jsonl"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                # Lines written before the scrubber existed are still on disk for 14 days,
                # so the renderer scrubs too rather than trusting the file.
                try:
                    out.append(json.loads(scrub(line)))
                except json.JSONDecodeError:
                    continue
    return out


# `trace last`: the newest trace a viewer started, not what the plugin runs by itself. The Service polls
# `status` every 10 s and reads lists, accounts and covers on its own, each under a trace of its own, so "the newest
# trace" was a 2-line poll within seconds of a failed play. These commands only read, or are the Service's own
# housekeeping: a trace whose every `cli start` is one of them is skipped. A name maps to its housekeeping
# sub-actions (the first word after it), or to None for every form of it.
HOUSEKEEPING: dict[str, frozenset | None] = {
    "status": None, "watching": None, "list": None, "top": None, "airing": None, "search": None, "media": None,
    "accounts": None, "torbox-user": None, "rd-user": None, "anizip": None, "episodes": None, "covers": None,
    "thumbs": None, "flush-progress": None, "tick": None, "picker-notice": None, "log-event": None, "trace": None,
    # The pending downloads' watcher and a notification's waiting child run in the background, on their own.
    "watch": None, "notify-wait": None,
    # The next episode's download at TorBox, which the tick starts by itself (`prefetch`).
    "prefetch": None,
    "pin": frozenset({"show"}), "groupstats": frozenset({"report"}), "subs": frozenset({"status", "prune"}),
    # find-local runs by itself when Accounts opens with no server.
    "torznab": frozenset({"list", "find-local"}),
    # The bar's window and transport buttons; `start` and `restart` park a player and stay.
    "player": frozenset({"status", "show", "hide", "toggle", "pause", "stop", "sub-delay"}),
}
# The words each of those commands' parsers accept as what it does (its `--help`'s first `{…}`), and the parser's
# default where the word is optional. An option's value (`player --set 1.5 sub-delay`) is none of them.
SUBACTIONS: dict[str, tuple[str, ...]] = {
    "player": ("start", "restart", "show", "hide", "toggle", "pause", "resume", "stop", "status", "sub-delay"),
    "torznab": ("list", "add", "check", "remove", "find-local"),
    "pin": ("show", "forget"),
    "groupstats": ("report", "backfill"),
    "subs": ("find", "fetch", "external", "prune", "status"),
}
DEFAULT_ACTION = {"groupstats": "report"}
# `releases` without --refresh or --query serves the stored search: the list a refused play opens by itself.
RELEASES_ACTIONS = ("--refresh", "--query")
# Service.qml's `run("healPlayer", ["player", "start"])`: the Service replacing a player that stopped answering.
SERVICE_TRACE_PREFIXES = ("ui-healPlayer-",)


def _command(entry: dict) -> tuple[str, list[str]]:
    """A `cli start` line's command and the words after it (`argv` minus `--trace <id>`)."""
    data = entry.get("data") or {}
    cmd = str(data.get("cmd") or "")
    argv = [str(a) for a in data.get("argv") or []]
    i = 0
    while i < len(argv) and argv[i] != cmd:
        i += 2 if argv[i] == "--trace" else 1
    return cmd, argv[i + 1:]


def _action(cmd: str, rest: list[str]) -> str | None:
    """What `cmd` was asked to do: the first word of `rest` that is one of its action words, else the parser's
    default. An option's value that is also an action word (`torznab --name list add`) is read as one."""
    words = SUBACTIONS.get(cmd, ())
    return next((a for a in rest if a in words), DEFAULT_ACTION.get(cmd))


def is_housekeeping(entry: dict) -> bool:
    """Is this `cli start` line a command the plugin runs by itself, or one that only reads?"""
    if str(entry.get("trace") or "").startswith(SERVICE_TRACE_PREFIXES):
        return True
    cmd, rest = _command(entry)
    if cmd == "releases":
        return not any(a.split("=")[0] in RELEASES_ACTIONS for a in rest)
    if cmd not in HOUSEKEEPING:
        return False
    subs = HOUSEKEEPING[cmd]
    return subs is None or _action(cmd, rest) in subs


def _label(starts: list[dict]) -> str:
    """What a skipped trace was, for the header: its first command other than `log-event`."""
    for entry in starts:
        cmd, rest = _command(entry)
        if cmd == "log-event":
            continue
        action = _action(cmd, rest)
        return f"{cmd} {action}" if HOUSEKEEPING.get(cmd) and action else cmd
    return "ui event"


# What `trace last` picks, as the `trace` help and the fallback note say it: the rule, with examples, not a closed list.
STARTED = ("something you started yourself, such as a play, a resolve, a sign-in or sign-out, a change in Accounts, a "
           "progress write to AniList, a player start or restart, a new search in the release list (`r`, or your own "
           "words) or a subtitle fetch")
# What `trace last` passes over, in the words its header and the `trace` help use: none of it is a command a viewer
# started, though the window buttons and the release list are the viewer's clicks.
SKIPPED = ("what the plugin runs by itself, commands that only read, the release list, and the window and play/pause "
           "buttons")
# The line `trace last` writes about its own choice, under its own trace.
PICK_COMPONENT, PICK_STAGE = "trace", "last"


def _own_trace(starts: list[dict]) -> bool:
    """Is this a `trace` run's own trace? The one rule, for the pick, the count and the fallback: the trace command
    writes no `cli start` (only its `trace/last` line, or the `internal/fail` of a run that crashed, a broken pipe
    under `| head`), and an older build logged one with `cmd: trace`. Every other command logs a start."""
    return not starts or all(_command(e)[0] == "trace" for e in starts)


def pick_last(lines: list[dict]) -> dict:
    """`trace last`'s choice: the trace that began last among those holding a command a viewer starts (a play, a
    resolve, a sign-in, an Accounts change, a player start), ordered by each trace's first line, so a running play's
    ticks do not lift it above what was started after it. `skipped_by` counts the newer traces passed over. With no
    such trace, the trace with the newest line (`rule: "newest"`, the earlier rule). A `trace` run's own
    trace is none of these (`_own_trace`)."""
    first: dict[str, int] = {}
    newest: dict[str, int] = {}
    starts: dict[str, list[dict]] = {}
    for i, entry in enumerate(lines):
        trace = entry.get("trace")
        if not trace:
            continue
        first.setdefault(trace, i)
        newest[trace] = i
        if entry.get("component") == "cli" and entry.get("stage") == "start":
            starts.setdefault(trace, []).append(entry)
    candidates = [t for t in first if not _own_trace(starts.get(t, []))]
    acted = [t for t in candidates if not all(is_housekeeping(e) for e in starts[t])]
    if not acted:
        last = max(candidates, key=newest.__getitem__, default=None)
        return {"trace": last, "rule": "newest" if last else "none", "skipped": 0, "skipped_by": {}}
    picked = max(acted, key=first.__getitem__)
    counts: dict[str, int] = {}
    for trace in candidates:
        if first[trace] > first[picked]:
            label = _label(starts[trace])
            counts[label] = counts.get(label, 0) + 1
    skipped_by = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return {"trace": picked, "rule": "action", "skipped": sum(counts.values()), "skipped_by": skipped_by}


def last_note(pick: dict) -> str:
    """The line `trace last` prints first when it passed over newer traces, fell back, or found nothing; else ""."""
    if pick["rule"] == "none":
        return f"trace last: there is no trace in the last {LAST_DAYS} days' log yet."
    if pick["rule"] == "newest":
        return (f"trace last: nothing you started yourself is in the last {LAST_DAYS} days' log, so this is the "
                "newest trace of any kind.")
    if not pick["skipped"]:
        return ""
    kinds = ", ".join(f"{label} ×{n}" for label, n in pick["skipped_by"].items())
    return (f"trace last: the newest trace you started. Skipped {pick['skipped']} newer ones: {SKIPPED} ({kinds}); "
            "`omarchy-anime trace <id>` renders any trace.")


def _failure_text(entry: dict) -> str | None:
    """What went wrong on a line, in its own words: `data.error`, else `data.reason`; None when it has neither.
    `fail_internal` writes `StopIteration: ` for an exception without a message, so the bare colon goes."""
    data = entry.get("data") or {}
    for key in ("error", "reason"):
        text = str(data.get(key) or "").strip().rstrip(":").strip()
        if text:
            return text
    return None


def _summary(e: dict, t0: float) -> dict:
    """The summary of one line: where it is, when (from `t0`, the trace's first line), and what it said went wrong."""
    return {"component": e.get("component"), "stage": e.get("stage"), "level": e.get("level"),
            "result": e.get("result"), "t_ms": round((_parse_ts(e.get("ts")) - t0) * 1000),
            "error": _failure_text(e), "suggest": e.get("suggest") or None}


def failure_line(stages: list[dict], t0: float) -> dict | None:
    """The line that ended a failed command: the first error-level line. Every command failure is written at error
    level (`status.fail_stage`, `status.fail_internal`) and the command stops there. None when the trace has none."""
    for e in stages:
        if e.get("level") == "error":
            return _summary(e, t0)
    return None


def unrecovered_line(stages: list[dict], t0: float) -> dict | None:
    """The last `result=FAIL` line that no later line of the same component and stage recovered, a recovery being
    any later line of that component/stage whose result is set and is not FAIL: a request retried into a PASS, or a
    feed whose sibling feeds answered. It is only worth showing in a trace with no `failure_line`, where nothing says
    the command failed (a command that played, or one killed before it could write its error). None when every FAIL
    was recovered."""
    recovered: set[tuple] = set()
    for e in reversed(stages):
        where = (e.get("component"), e.get("stage"))
        if e.get("result") == "FAIL" and where not in recovered:
            return _summary(e, t0)
        if e.get("result"):
            # Any result: a FAIL that gets here is of a component/stage a later line already recovered.
            recovered.add(where)
    return None


def render(lines: list[dict], trace_id: str) -> dict:
    stages = [e for e in lines if e.get("trace") == trace_id]
    if not stages:
        return {"trace": trace_id, "stages": [], "total_ms": 0, "failure": None, "unrecovered": None}
    t0 = _parse_ts(stages[0].get("ts"))
    out = []
    for e in stages:
        rec = {
            "t_ms": round((_parse_ts(e.get("ts")) - t0) * 1000),
            "level": e.get("level"),
            "component": e.get("component"),
            "stage": e.get("stage"),
            "result": e.get("result"),
            "elapsed_ms": e.get("ms"),
            "suggest": e.get("suggest"),
            "data": e.get("data"),
        }
        out.append(rec)
    total = round((_parse_ts(stages[-1].get("ts")) - t0) * 1000)
    # One of the two at most: a failed command's own line says why it failed, and a FAIL before it is not the answer.
    failure = failure_line(stages, t0)
    return {"trace": trace_id, "stages": out, "total_ms": total, "failure": failure,
            "unrecovered": None if failure else unrecovered_line(stages, t0)}


def _f(value) -> str:
    """One field of a row as printed: a service's control characters (ESC, OSC 52, a newline that would forge a row)
    shown as `\\xNN` (`core.escape_controls`), never acted on by the terminal."""
    return escape_controls(str(value), lines=False)


def format_text(report: dict) -> str:
    rows = [f"trace {_f(report['trace'])}  total {report['total_ms']} ms"]
    stages = report["stages"]
    i = 0
    while i < len(stages):
        s = stages[i]
        # Collapse runs of identical result-less lines (e.g. one "score/candidate" per release) into one row.
        j = i
        while (j + 1 < len(stages) and not s.get("result") and stages[j + 1].get("component") == s["component"]
               and stages[j + 1].get("stage") == s["stage"] and not stages[j + 1].get("result")):
            j += 1
        elapsed = f"{s['elapsed_ms']:.0f} ms" if isinstance(s.get("elapsed_ms"), (int, float)) else ""
        res = _f(s.get("result") or "")
        label = _f(s["stage"]) if j == i else f"{_f(s['stage'])} ×{j - i + 1}"
        rows.append(f"  +{s['t_ms']:>7} ms  {_f(s['component']):<22} {label:<16} {res:<8} {elapsed}")
        if s.get("suggest"):
            rows.append(f"             ↳ {_f(s['suggest'])}")
        i = j + 1
    if report.get("failure"):
        rows.append(f"failure: {_summary_text(report['failure'])}")
    elif report.get("unrecovered"):
        rows.append(f"unrecovered: {_summary_text(report['unrecovered'])}  "
                    "(no failure was logged; nothing later in the trace recovered this FAIL)")
    return "\n".join(rows)


def _summary_text(f: dict) -> str:
    """`<component>/<stage>`, then `: <error>` and `  → <suggest>` only when the line has them, never `None`."""
    row = f"{_f(f['component'])}/{_f(f['stage'])}"
    if f.get("error"):
        row += f": {_f(f['error'])}"
    if f.get("suggest"):
        row += f"  → {_f(f['suggest'])}"
    return row


def tail(follow_seconds: float = 0.0):
    """Yield new JSONL lines from today's file as they appear (best-effort follow)."""
    path = LOG_DIR / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
    pos = path.stat().st_size if path.exists() else 0
    deadline = time.monotonic() + follow_seconds if follow_seconds > 0 else None
    while True:
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                fh.seek(pos)
                scrub = redactor()                  # once per read: a token saved meanwhile is masked by the next
                for line in fh:
                    yield scrub(line.rstrip("\n"))
                pos = fh.tell()
        if deadline is None or time.monotonic() > deadline:
            return
        time.sleep(0.5)
