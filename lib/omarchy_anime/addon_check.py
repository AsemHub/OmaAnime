"""`omarchy-anime addons check <file>`: would this add-on load, and answer well?

The file is loaded by the registry's own `indexers.load_file`, wherever it is, so a refusal reads exactly as
Settings would show it. Then `episode_search` is asked once through the registry's own guarded `indexers.call`,
with an invented query, and what it returns goes through the same `indexers.normalize_releases` a play uses, one
record at a time, so each dropped record is named with its reason.

Offline (the default) `ctx.http` answers every request with `ctx.HttpError`, as for a host that cannot be reached.
A connection the add-on's own Python opens in this process through the usual socket calls (`getaddrinfo`,
`create_connection`, `socket.connect`, `connect_ex`) is refused and reported, while it loads (always) and while
`episode_search` runs (offline). It is a check, not a sandbox: other ways out (a datagram `sendto`, `_socket`) and a
program it starts are not watched. `--live` hands it the plugin's real client
for a title and episode the viewer names. Either way `ctx.cache_dir` is a scratch folder
removed afterwards, so a check leaves nothing behind.
"""
from __future__ import annotations

import contextlib
import errno
import io
import os
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from pathlib import Path

from . import addon_help, core, indexers, torznab
from . import say as saymod

# The invented episode the offline call is asked for. Its ids are empty: the interface says any may be missing.
OFFLINE_TITLE = "Lantern x Lantern"
OFFLINE_EPISODE = 3
# The functions an add-on may offer, as the interface lists them (`addon_help.MODULE`, without the two constants).
CAPABILITIES = tuple(n for f in addon_help.MODULE for n in f.names if n not in ("API_VERSION", "NAME"))
# How many releases and rejected records the answer lists; the counts are always whole.
LISTED = 25
SHOWN_TEXT = 200                # characters of printed output quoted in a problem
OFFLINE = "offline"             # `data["reason"]` of the offline answer: the `ctx.HttpError` `OfflineHttp` raises


class OfflineHttp:
    """`ctx.http` for the offline check. Every request is recorded (method and address, without its query, which
    may carry a key) and answered with nothing: `ctx.HttpError`, as for a host that cannot be reached."""

    def __init__(self):
        self.asked: list[dict] = []

    def request(self, component, method, url, **_kw):
        self._refuse(component, method, url)

    def get_capped(self, component, url, cap, timeout=None):
        self._refuse(component, "GET", url)

    def _refuse(self, component, method, url):
        self.asked.append({"component": str(component), "method": str(method), "url": shown_url(url)})
        raise core.StageError("addons.check", "no answer: the offline check sends no request",
                              "run the check with --live to ask the real indexer", reason=OFFLINE)


def shown_url(url) -> str:
    """An address without its query, fragment or user info, for the answer and the log: the parts that name
    the host and the endpoint, never a key."""
    try:
        s = urllib.parse.urlsplit(str(url))
        host = s.hostname or ""
        port = f":{s.port}" if s.port else ""
    except ValueError:
        return "<not an address>"
    return core.redact_text(f"{s.scheme}://{host}{port}{s.path}" if s.scheme else s.path)


@contextlib.contextmanager
def no_network(attempts: list, keep: dict):
    """Refuse every Internet connection while the offline call runs, and record where it was going: an add-on that
    opens one itself, outside `ctx.http`, would otherwise reach the network from a check that promises not to. When
    `keep["on"]` is set by then (the call ran past its time and is abandoned, not stopped), the refusal stays for the
    rest of the command."""
    real = (socket.getaddrinfo, socket.create_connection, socket.socket.connect, socket.socket.connect_ex)

    def refuse(where):
        attempts.append(str(where))
        raise OSError(errno.ENETUNREACH, "the offline add-on check opens no connection")

    def getaddrinfo(host, port, *a, **kw):
        refuse(f"{host}:{port}")

    def create_connection(address, *a, **kw):
        refuse(_address(address))

    def connect(self, address):
        if not isinstance(address, tuple):          # a local socket file, not the network
            return real[2](self, address)
        refuse(_address(address))

    def connect_ex(self, address):
        if not isinstance(address, tuple):
            return real[3](self, address)
        refuse(_address(address))
    socket.getaddrinfo, socket.create_connection = getaddrinfo, create_connection
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    try:
        yield
    finally:
        if not keep.get("on"):
            socket.getaddrinfo, socket.create_connection, socket.socket.connect, socket.socket.connect_ex = real


def _address(address) -> str:
    return f"{address[0]}:{address[1]}" if isinstance(address, tuple) and len(address) >= 2 else str(address)


class StdoutByThread:
    """`sys.stdout` once a load is abandoned: the thread that runs the command writes to the command's stdout (its JSON
    answer), and every other thread (the abandoned top level, and any thread it started) to stderr, as the loader
    sent its prints while it ran. Putting the plain stdout back instead let a top level still printing land inside the
    answer."""

    def __init__(self, answer, other, owner: int):
        self._answer, self._other, self._owner = answer, other, owner

    def _target(self):
        return self._answer if threading.get_ident() == self._owner else self._other

    def write(self, text):
        return self._target().write(text)

    def flush(self):
        self._target().flush()

    def __getattr__(self, name):
        return getattr(self._answer, name)


def load_within(path: Path, budget: float):
    """`indexers.load_file(path)` given `budget` seconds, as the call is: its answer, or None when the file's top
    level was still running then (it is abandoned, not stopped, as a call past its time is)."""
    box: dict = {}
    stdout = sys.stdout

    def run():
        box["loaded"] = indexers.load_file(path)
    worker = threading.Thread(target=run, name=f"addon-check-load-{path.name}", daemon=True)
    worker.start()
    worker.join(budget)
    if worker.is_alive():
        # The loader points stdout at stderr while it runs; the answer goes to stdout, the abandoned top level's
        # prints stay on stderr (`StdoutByThread`).
        sys.stdout = StdoutByThread(stdout, sys.stderr, threading.get_ident())
        return None
    return box.get("loaded") or (None, "could not be loaded: its top level stopped the load")


def episode_query(title: str, episode: int, packs: bool) -> indexers.EpisodeQuery:
    """The query one call is asked: the title as the show's only title, no ids, no season. The search asks
    each call a copy (`for_call`), and so does the check."""
    return indexers.EpisodeQuery(ids={}, episode=int(episode), variants=[title], series_title=title, alt_numbers=[],
                                 season=None, packs_allowed=bool(packs), titles=(title,)).for_call(frozenset())


def _where(exc: BaseException, file: Path) -> str:
    """`wren.py:42 in _ask`: the add-on's own last line the error passed through, or "" when none was."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename).name == file.name]
    return f"{file.name}:{frames[-1].lineno} in {frames[-1].name}" if frames else ""


_DROPPED = {"not_a_dict": "it is not a dict", "title": "it has no title (a string that is not empty)",
            "info_hash": "its info_hash is not 40 hex characters"}


def _records(found: list, problem, note, kind: str) -> list[dict]:
    """Each record through the play's own normaliser, alone, so a dropped one is named with its reason. The kept
    records, as a play would read them."""
    kept: list[dict] = []
    for i, record in enumerate(found):
        title = record.get("title") if isinstance(record, dict) else None
        named = f"{kind} {i}" + (f" ({str(title)[:80]!r})" if title else "")
        ok, report = indexers.normalize_releases([record])
        for why in report.get("dropped", {}):
            example = report.get("examples", {}).get(why, "")
            problem(f"dropped_{kind}", f"{named} is dropped: {_DROPPED.get(why, why)} (got {example})")
        for key in report.get("coerced", {}):
            note("coerced", f"{named}: {key} {report['examples'][key]} is read as a number")
        for key in report.get("refused", {}):
            note("not_a_number", f"{named}: {key} {report['examples'][key]} is not a number, so it is unknown")
        if ok and kind == "release" and not ok[0].get("seeders"):
            note("no_seeders", f"{named} has no seeders count, so a play sets it aside as nobody sharing it")
        kept.extend(ok)
    return kept


def check(path: Path, log: core.Log, settings: dict, *, call: bool = True, live: bool = False,
          title: str | None = None, episode: int = 1, timeout: float | None = None) -> dict:
    """The answer of `addons check`: `ok` only when the file would load and its search answered well.
    `problems` say what would go wrong, `notes` what is worth knowing; each is `{code, message}`."""
    t0 = time.monotonic()
    problems: list[dict] = []
    notes: list[dict] = []

    def problem(code, message):
        problems.append({"code": code, "message": message})

    def note(code, message):
        notes.append({"code": code, "message": message})
    path = Path(path).expanduser()
    shown = saymod.shown_path(str(path))
    out: dict = {"ok": False, "file": str(path), "name": None, "offers": [], "called": False, "live": live,
                 "query": None, "problems": problems, "notes": notes, "requests": [], "releases": [],
                 "release_count": 0, "rejected": []}

    def done(stage: str) -> dict:
        out["ok"] = not problems
        log.emit("info" if out["ok"] else "warn", "addons", "check", result="PASS" if out["ok"] else "FAIL",
                 ms=(time.monotonic() - t0) * 1000, file=str(path), name=out["name"], stopped_at=stage,
                 called=out["called"], live=live, problems=[p["code"] for p in problems],
                 notes=[n["code"] for n in notes], releases=out["release_count"], requests=len(out["requests"]),
                 # What the add-on asked for (offline, each answered with nothing): address only, no query or user.
                 asked=[f'{r["method"]} {r["url"]}' for r in out["requests"][:LISTED]],
                 suggest="; ".join(p["message"] for p in problems)[:600] if problems else "")
        return out

    if not path.is_file():
        problem("not_a_file", f"{shown} is not a file")
        return done("file")
    if not indexers.is_addon_name(path.name):
        problem("not_an_addon_name", f"{path.name} would not be loaded: the plugin loads only *.py files whose "
                                     "name does not start with _ or .")
    folder = indexers.indexers_dir()
    try:
        inside = path.resolve().parent == folder.resolve()
    except OSError:
        inside = False
    if Path(os.path.abspath(path)).parent == folder or inside:    # what `load` would refuse there
        unsafe = indexers.folder_refusal(folder) or indexers.file_refusal(path, folder)
        if unsafe:
            problem("unsafe", unsafe if unsafe.startswith(("~", "/")) else f"{path.name} {unsafe}")
    if not inside:
        note("outside_folder", f"the plugin loads add-ons from {saymod.shown_path(str(folder))}; this file was "
                               "checked where it is")
    if path.stem == torznab.STEM:
        if torznab.load_feeds().sources:
            problem("torznab_name", "the name torznab is the built-in Torznab add-on's while a Torznab server is set "
                                    "up, so this file is not loaded; rename it")
        else:
            note("torznab_name", "the name torznab is the built-in Torznab add-on's: once a Torznab server is set "
                                 "up, this file is not loaded")

    # The top level runs with the connections refused too, `--live` and `--no-call` included: it must never do
    # network work (it runs every time a command loads the folder, outside any time limit).
    budget = float(timeout) if timeout is not None else indexers.budget(settings)
    keep: dict = {}
    loading: list[str] = []
    with no_network(loading, keep):
        loaded = load_within(path, budget)
        keep["on"] = loaded is None             # abandoned while loading: it gets no connection later either
    if loaded is None:
        problem("load_timeout", f"{path.name} took longer than {budget:g} s to load: every command that loads the "
                                "folder waits for its top level, with no time limit; keep slow and network work "
                                "out of it")
        log.warn("addons", "check.load", result="FAIL", file=str(path), budget_s=budget, connections=loading,
                 suggest=problems[-1]["message"])
        return done("load")
    indexer, reason = loaded
    log.emit("info" if indexer else "warn", "addons", "check.load", result="PASS" if indexer else "FAIL",
             file=str(path), name=indexer.name if indexer else None, reason=reason or None, connections=loading,
             suggest=f"fix {path.name}: {reason}" if reason else "")
    if loading:
        problem("own_connection", f"{path.name} opened a connection while loading ({', '.join(loading[:5])}): its "
                                  "top level runs every time a command loads the folder, so it must do no network "
                                  "work; ask in episode_search, through ctx.http")
    if indexer is None:
        problem("not_loaded", f"{path.name} is not loaded: {reason}")
        return done("load")
    out["name"] = indexer.name
    out["offers"] = [c for c in CAPABILITIES if indexer.offers(c)]
    pair = [f for f in indexers.SUBTITLE_TRACK_FUNCTIONS if indexer.offers(f)]
    if len(pair) == 1:
        other = next(f for f in indexers.SUBTITLE_TRACK_FUNCTIONS if f not in pair)
        problem("half_pair", f"{path.name} has {pair[0]} but no {other}: the two count only together, so neither "
                             "is asked")
    if not call:
        return done("loaded")

    if live:
        query = episode_query(str(title), episode, core.as_bool(settings.get("allowBatches"), True))
        http = core.Http(log, settings["httpTimeout"])
    else:
        query = episode_query(OFFLINE_TITLE, OFFLINE_EPISODE, True)
        http = OfflineHttp()
    out["query"] = {"series_title": query.series_title, "episode": query.episode, "pack_pass": query.pack_pass,
                    "packs_allowed": query.packs_allowed, "budget_s": budget}
    printed = io.StringIO()
    attempts: list[str] = []
    failures: list = []
    with tempfile.TemporaryDirectory(prefix="omaanime-addon-check-") as scratch:
        ctx = indexers.context(http, log, settings, cache_root=Path(scratch))
        guard = contextlib.nullcontext() if live else no_network(attempts, keep)
        with contextlib.redirect_stdout(printed), guard:
            value = indexers.call(indexer, "episode_search", log, ctx, query, budget_s=budget,
                                  about={"episode": query.episode, "pack_pass": query.pack_pass, "check": True},
                                  failures=failures)
            keep["on"] = any(getattr(exc, "call_failure", "") == "timeout" for _, exc in failures)
    out["called"] = True
    out["requests"] = list(getattr(http, "asked", []))
    if printed.getvalue():
        text = printed.getvalue()
        problem("printed", f"episode_search printed {len(text)} characters ({text[:SHOWN_TEXT]!r}): while it "
                           "runs, standard output is the command's JSON answer, so a play would break; log with "
                           "ctx.log instead")
    if attempts:
        problem("own_connection", f"episode_search opened a connection itself ({', '.join(attempts[:5])}), "
                                  "outside ctx.http: use ctx.http.request, which retries, logs and masks keys")
    for _, exc in failures:
        where = _where(exc, path)
        at = f" at {where}" if where else ""
        own = getattr(exc, "call_failure", "")
        if own == "timeout":
            problem("timeout", f"episode_search did not answer within {budget:g} s: a play skips it then; keep "
                               "your own timeouts short and never wait without one")
        elif own == "wrong_type":
            detail = getattr(exc, "call_detail", {})
            problem("wrong_type", f"episode_search returned {detail.get('returned')}, not a {detail.get('wanted')}: "
                                  "it must return a list of release dicts")
        elif not live and isinstance(getattr(exc, "data", None), dict) and exc.data.get("reason") == OFFLINE:
            note("http_error", f"episode_search re-raised ctx.HttpError for the offline answer{at}: a play then "
                               "says the indexer could not be reached, in the plugin's own words")
        else:
            problem("raised", f"episode_search raised {type(exc).__name__}: {exc}{at}")
    if isinstance(value, list):
        kept = _records(value, problem, note, "release")
        rejected = _records([rel for rel, _, _ in query.rejected], problem, note, "rejected")
        out["release_count"] = len(kept)
        out["releases"] = kept[:LISTED]
        why = {id(rel): (code, reason) for rel, code, reason in query.rejected}
        out["rejected"] = [{"title": r.get("title"), "code": why.get(id(r), ("", ""))[0],
                            "why": why.get(id(r), ("", ""))[1]} for r in rejected[:LISTED]]
        log.emit("info" if kept or not value else "warn", "addons", "check.releases",
                 result="PASS" if kept else "EMPTY", returned=len(value), kept=len(kept),
                 rejected=len(query.rejected), live=live)
        if live and not kept:
            note("empty", "no release came back for this title and episode: try one the indexer has")
    return done("call")


def summary(out: dict) -> str:
    """The answer in a few lines for a person (stderr), the JSON being on stdout."""
    name = Path(out.get("file") or "").name
    lines = []
    if out.get("ok"):
        what = f"loads as {out.get('name')} (offers {', '.join(out.get('offers') or [])})"
        if out.get("called"):
            how = "the live search" if out.get("live") else "the offline search"
            count = out.get("release_count", 0)
            what += f" and answered {how} with {count} release{'' if count == 1 else 's'}"
        lines.append(f"PASS: {name} {what}.")
    else:
        count = len(out.get("problems") or [])
        lines.append(f"FAIL: {name}: {count} problem{'s' if count != 1 else ''}.")
    lines += [f"- {p['message']}" for p in out.get("problems") or []]
    lines += [f"note: {n['message']}" for n in out.get("notes") or []]
    return "\n".join(lines) + "\n"
