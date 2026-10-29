"""Torznab servers on the Accounts sheet: list, add, check, remove.

The servers file has one owner, `torznab_servers` (read, keys, write, and its lock); this module is its one caller
that changes it. Every change is a read-modify-write under the file's lock, re-read after any network call, and
read back after the write: the list as written, at mode 600. Fields this version does not know are kept.

A server is added with its address and API key, checked first (`torznab.check`, the same pool, lines and remembered
verdicts as a play, under a short budget), and saved when the check passed, or (a new server only) failed for a
passing reason (no answer, a timeout, a rate limit, a 5xx). A failed key change for a listed server puts back what
the add-on remembered about it. Removing a server also has the add-on forget it (`torznab.forget`).

The sheet names a row by its id (`<n>-<hash of the stored address and key's fingerprint>`, from `listing`), never
by its address, so a hand-written address (a password, a key) is never on a command line, and a duplicate row
acts on itself alone. What leaves this module holds no key: a key of 16 characters or more shows its last four
(`mask_key`), a shorter one none, and the key is registered with the log scrubber (`core.remember_secret`) before
any call that could echo it. Nothing here writes `shell.json`.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import ipaddress
import os
import stat
import time
import urllib.parse

from . import core, say, torznab, torznab_servers

COMPONENT = "accounts.torznab"
# The Accounts check's own budget: short enough for a sheet, long enough for a Prowlarr's list and a handful of
# caps calls (a blackholed host costs this much, never `indexerTimeoutSeconds`' 60 s). The sheet reads it from
# the `listing` answer (`checkBudget`), so its "up to N s" cannot drift.
CHECK_BUDGET_S = 20.0
CHECK_BUDGET_BOUNDS = (3.0, 120.0)
MASK_MIN_LEN = 16                     # a key shorter than this shows none of its characters


# Said in Accounts (`words.lines`) after a check or an add of a server reached over plain http on another machine.
# Kept, not refused: a Prowlarr on the home network is a normal setup, and the user decides what that path is.
PLAIN_HTTP_LINE = ("This server is reached over plain http:// on another machine, so its API key travels unencrypted; "
                   "use https:// unless that network is only yours.")


def plain_http_remote(url: str) -> bool:
    """An `http://` address whose host is not this machine (`localhost`, `*.localhost`, 127.0.0.0/8, ::1). Read from
    the address alone: no name is looked up."""
    try:
        p = urllib.parse.urlsplit(str(url or ""))
        host = (p.hostname or "").lower()
    except ValueError:
        return False
    if p.scheme.lower() != "http" or not host or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def _plain_http(log, server: torznab_servers.Server, words: dict, stage: str) -> dict:
    """`words` with `PLAIN_HTTP_LINE` added, and one WARN, when the server is plain http on another machine."""
    if not plain_http_remote(server.url):
        return words
    src, _ = torznab.source_of(server)
    log.warn(COMPONENT, "plain_http", result="WARN", server=src.label if src else core.redact_text(server.url),
             during=stage, suggest="the API key is sent unencrypted to this server; use https:// if it can")
    return {**words, "lines": [*(words.get("lines") or []), PLAIN_HTTP_LINE]}


def mask_key(key: str) -> str:
    """A key as the sheet shows it: `…` and its last four characters when it has 16 or more, `…` alone when
    shorter (four of eight would be half of it), "" for none."""
    key = str(key or "")
    if not key:
        return ""
    return "…" + key[-4:] if len(key) >= MASK_MIN_LEN else "…"


def _row(server: torznab_servers.Server) -> str:
    """The row's stable part: a hash of the stored address and the key's fingerprint (never the key)."""
    raw = "torznab-row:" + server.url + "\0" + torznab._fingerprint(server.key)
    return hashlib.sha256(raw.encode()).hexdigest()[:10]


def _source(server: torznab_servers.Server):
    src, _ = torznab.source_of(server)
    return src


def _same(a: torznab_servers.Server, b: torznab_servers.Server) -> bool:
    """One server: the same base once normalised (a trailing `/`, the scheme's case), or the same Torznab feed."""
    sa, sb = _source(a), _source(b)
    return bool(sa and sb and (sa.base == sb.base or sa.feed.url == sb.feed.url))


def find(servers: list, *, row_id: str = "", url: str = "") -> int | None:
    """The index of the one entry a command acts on, or None. By `row_id` (`<n>-<row>`): entry n when its row still
    matches, else the first entry with that row (the file changed under the sheet). By `url` (a terminal): the
    first entry whose stored or shown address is exactly it, else the first that is the same server (`_same`)."""
    if row_id:
        n, _, row = str(row_id).partition("-")
        if n.isdigit() and 1 <= int(n) <= len(servers) and _row(servers[int(n) - 1]) == row:
            return int(n) - 1
        return next((i for i, s in enumerate(servers) if _row(s) == row), None)
    url = str(url or "").strip()
    if not url:
        return None
    exact = next((i for i, s in enumerate(servers) if url in (s.url.strip(), core.redact_text(s.url.strip()))), None)
    if exact is not None:
        return exact
    wanted = torznab_servers.Server(url=url)
    return next((i for i, s in enumerate(servers) if _same(s, wanted)), None)


def _shown(server: torznab_servers.Server, n: int | None = None) -> dict:
    """One server as the sheet shows it: its id, the address (user info and any key in it masked), its name, the
    key masked, and what the add-on remembers it to be (no network). A server the add-on skips says why."""
    out = {"url": core.redact_text(server.url), "name": server.name, "key": mask_key(server.key),
           "hasKey": bool(server.key), "row": _row(server)}
    if n is not None:
        out["id"] = f"{n}-{out['row']}"
    src, why = torznab.source_of(server)
    if src is None:
        return {**out, "label": "", "kind": None, "feeds": 0, "problem": why}
    feeds, record, _ = torznab.torznab_prowlarr.known_feeds(src)
    return {**out, "label": src.label, "kind": record if record in ("prowlarr", "torznab") else None,
            "feeds": len(feeds), "problem": ""}


def listing() -> dict:
    """The servers file as the Accounts sheet shows it, read fresh (no cache, no network): `servers` in file order
    (each with its `id`), `skipped` the entries the file itself could not read, `problem` why the whole file is
    unusable, `caption` the sheet's line above the rows, `checkBudget` the check's budget in seconds."""
    got = torznab_servers.read()
    servers, seen = [], set()
    for n, s in enumerate(got.servers, 1):
        entry = _shown(s, n)
        src = _source(s)
        if src is not None and src.feed.url in seen:
            entry["problem"] = "the same feed is listed earlier"
        if src is not None:
            seen.add(src.feed.url)
        servers.append(entry)
    path = str(torznab_servers.path())
    return {"path": path, "problem": got.problem, "skipped": list(got.skipped), "servers": servers,
            "caption": say.torznab_caption(got.problem, got.skipped, path, len(servers)),
            "checkBudget": CHECK_BUDGET_S}


def _changeable(got: torznab_servers.ServersFile, stage: str) -> None:
    """A file the sheet may rewrite: one it read whole. A refused file (readable by others, not JSON, another
    version) or one with an entry it could not read would lose those entries on a rewrite, so it is refused."""
    if got.problem:
        raise core.StageError(stage, "The Torznab servers file cannot be changed from here: " + got.problem,
                              f"fix or remove {torznab_servers.path()}, then try again")
    if got.skipped:
        first = got.skipped[0]
        raise core.StageError(stage, f"Server {first['server']} in the Torznab servers file cannot be read "
                                     f"({first['reason']}); saving would drop it",
                              f"fix or remove it in {torznab_servers.path()}, then try again")


def _save(got: torznab_servers.ServersFile, servers: list, extras: list, log, stage: str) -> str:
    """Write the list (`torznab_servers.write`, keeping the fields this version does not read), then read it back
    and check its mode. The caller holds the lock. Returns the mode; raises when it did not come back."""
    t0 = time.monotonic()
    path = torznab_servers.write(servers, extras, got.top)
    try:
        mode = f"{stat.S_IMODE(os.lstat(path).st_mode):o}"
    except OSError:
        mode = "?"
    back = torznab_servers.read()
    ok = (not back.problem and not back.skipped and list(back.servers) == list(servers)
          and list(back.extras) == list(extras) and mode == "600")
    log.emit("info" if ok else "error", COMPONENT, "save", result="PASS" if ok else "FAIL",
             ms=(time.monotonic() - t0) * 1000, path=str(path), mode=mode, servers=len(servers),
             read_back=len(back.servers), suggest="" if ok else f"{path} did not read back as written")
    if not ok:
        raise core.StageError(stage, f"{path} did not read back as written", "check the filesystem, then try again")
    return mode


def _forget(log, server: torznab_servers.Server, keep: list, why: str) -> int:
    """`torznab.forget`, logged: what was deleted, and each file that could not be."""
    removed, failed = torznab.forget(server, keep)
    src = _source(server)
    log.emit("warn" if failed else "debug", COMPONENT, "forget", result="FAIL" if failed else "PASS",
             server=src.label if src else "", why=why, removed=removed, failed=failed,
             suggest="delete these files by hand; the add-on would otherwise use them" if failed else "")
    return len(removed)


# ----------------------------------------------------------------------------- commands

def _budget(budget_s) -> float:
    lo, hi = CHECK_BUDGET_BOUNDS
    try:
        value = float(budget_s)
    except (TypeError, ValueError):
        return CHECK_BUDGET_S
    return min(hi, max(lo, value)) if value == value else CHECK_BUDGET_S


def _check_one(http, log, server: torznab_servers.Server, n: int, settings: dict, budget_s) -> dict:
    core.remember_secret(server.key)
    result = torznab.check(http, log, server, settings, budget_s=_budget(budget_s))
    return {"url": core.redact_text(server.url), "id": f"{n}-{_row(server)}", "row": _row(server), **result,
            "words": _plain_http(log, server, say.torznab_words(result), "check")}


def check(http, log, settings: dict, url: str | None = None, budget_s=CHECK_BUDGET_S, row_id: str = "") -> dict:
    """Check one server (by its row id, or its address) or every server side by side, each under `budget_s`. What
    each is and what its feeds are is remembered for the plays that follow (`torznab.check`); the file is not
    written."""
    got = torznab_servers.read()
    if got.problem:
        raise core.StageError("torznab.check", "The Torznab servers file cannot be used: " + got.problem,
                              f"fix or remove {torznab_servers.path()}")
    picked = list(enumerate(got.servers, 1))
    if url or row_id:
        at = find(got.servers, row_id=row_id, url=url or "")
        if at is None:
            log.info(COMPONENT, "check", result="SKIP", asked=row_id or core.redact_text(str(url)),
                     reason="not_in_file", why="no such server in the file")
            raise core.StageError("torznab.check", say.TORZNAB_GONE.rstrip("."), "`omarchy-anime torznab list`")
        picked = [(at + 1, got.servers[at])]
    t0 = time.monotonic()
    if len(picked) <= 1:
        out = [_check_one(http, log, s, n, settings, budget_s) for n, s in picked]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(picked))) as pool:
            out = list(pool.map(lambda p: _check_one(http, log, p[1], p[0], settings, budget_s), picked))
    log.info(COMPONENT, "check", result="PASS" if all(r["ok"] for r in out) else "FAIL",
             ms=(time.monotonic() - t0) * 1000, servers=len(out), failing=[r["server"] for r in out if not r["ok"]])
    return {"servers": out}


def add(http, log, settings: dict, url: str, key: str, name: str = "", budget_s=CHECK_BUDGET_S) -> dict:
    """Add a server, or give a listed one a new key: checked first (outside the lock), then, under the lock, the
    file read again and the entry found again, and saved when the check passed or (a new server only) failed for a
    passing reason. Read back at mode 600. A server not saved is forgotten by the add-on when it is not in the
    file, and a listed one gets back what the add-on remembered about it before this check."""
    t0 = time.monotonic()
    url, key, name = str(url or "").strip(), str(key or "").strip(), str(name or "").strip()
    core.remember_secret(key)
    _changeable(torznab_servers.read(), "torznab.add")
    if not url:
        raise core.StageError("torznab.add", "No address given", "paste the server's address")
    if key and len(key) < torznab_servers.MIN_KEY_LEN:
        raise core.StageError("torznab.add", f"That API key is shorter than {torznab_servers.MIN_KEY_LEN} characters",
                              "copy the whole key; Prowlarr and Jackett keys are 32 characters")
    server = torznab_servers.Server(url=url, key=key, name=name)
    src, why = torznab.source_of(server)
    if src is None:
        raise core.StageError("torznab.add", say.sentence(why), "")
    listed = find(torznab_servers.read().servers, url=url) is not None
    before = torznab.snapshot(server) if listed else {}
    result = torznab.check(http, log, server, settings, budget_s=_budget(budget_s))
    with torznab_servers.locked():
        got = torznab_servers.read()
        _changeable(got, "torznab.add")
        at = find(got.servers, url=url)
        save = bool(result["ok"] or (result["transient"] and at is None))
        mode = ""
        if save:
            servers, extras = list(got.servers), list(got.extras)
            if at is None:
                servers.append(server)
                extras.append({})
            else:
                servers[at] = torznab_servers.Server(url=url, key=key, name=name or servers[at].name)
            mode = _save(got, servers, extras, log, "torznab.add")
    caches, restored = 0, []
    if not save and at is None:
        caches = _forget(log, server, got.servers, "not saved")
    elif not save:
        restored = torznab.restore(before)
        log.emit("warn" if restored else "debug", COMPONENT, "restore", result="FAIL" if restored else "PASS",
                 server=src.label, files=len(before), failed=restored,
                 suggest="a Check of this server puts its remembered detection right" if restored else "")
    log.info(COMPONENT, "add", result="PASS" if result["ok"] else "PARTIAL" if save else "FAIL",
             ms=(time.monotonic() - t0) * 1000, server=src.label, replaced=save and at is not None,
             kind=result["kind"], problem=result["problem"] or None, transient=result["transient"], saved=save,
             suggest="" if save else "not saved: " + torznab.suggest(result["problem"]))
    return {"saved": save, "replaced": save and at is not None, "mode": mode, "server": _shown(server),
            "check": {"url": core.redact_text(server.url), "row": _row(server), **result},
            "words": _plain_http(log, server, say.torznab_words(result, saved=save), "add"), "caches": caches}


def remove(log, url: str = "", row_id: str = "") -> dict:
    """Remove the one server a row (or an address) names, under the lock, read the file back, and have the
    add-on forget it (`torznab.forget`, keeping any feed another server searches). A server that is no longer
    there is `removed: false`, with the sheet's sentence for it."""
    t0 = time.monotonic()
    with torznab_servers.locked():
        got = torznab_servers.read()
        _changeable(got, "torznab.remove")
        at = find(got.servers, row_id=row_id, url=url)
        if at is None:
            log.info(COMPONENT, "remove", result="SKIP", ms=(time.monotonic() - t0) * 1000,
                     asked=row_id or core.redact_text(str(url)), servers=len(got.servers),
                     reason="not_in_file", why="no such server in the file", suggest="the list is read again")
            return {"removed": False, "caches": 0, "servers": len(got.servers),
                    "words": {"headline": say.TORZNAB_GONE, "action": "", "lines": []}}
        gone = got.servers[at]
        keep = got.servers[:at] + got.servers[at + 1:]
        mode = _save(got, keep, got.extras[:at] + got.extras[at + 1:], log, "torznab.remove")
    caches = _forget(log, gone, keep, "removed")
    src = _source(gone)
    log.info(COMPONENT, "remove", result="PASS", ms=(time.monotonic() - t0) * 1000, servers=len(keep),
             caches=caches, server=src.label if src else core.redact_text(gone.url))
    return {"removed": True, "mode": mode, "caches": caches, "servers": len(keep)}
