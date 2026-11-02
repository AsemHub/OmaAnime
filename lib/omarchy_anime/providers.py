"""Stream providers: TorBox (primary), Real-Debrid (secondary). The plugin never streams a torrent itself."""
from __future__ import annotations

import json
import math
import random
from typing import NamedTuple
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import core
from .score import (pick_episode_file, pick_refused_suggest, skipped_summary, set_aside_words, FILE_FRANCHISE_SHOW,
                    FILE_OTHER_SEASON, FILE_EXTRAS_FOLDER)
from .core import (Http, Log, StageError, BlockedError, Interrupted, ProviderUnavailableError, cached_json, write_cache,
                   RD_URL, TORBOX_URL, CACHE_DIR, USER_AGENT, as_int, DEFAULT_SETTINGS, pack_files,
                   FILES_COMPLETE, FILES_FAILED, FILES_UNKNOWN, LARGE_MAX_BYTES)

# ----------------------------------------------------------------------------- the block guard
#
# `BlockedError` says "this provider refuses *this release*", which is why a 451 has never
# consumed the probe budget. What nothing counted was how many in a row: one blocked release is the
# release, N consecutive blocked releases are the account, the plan, or the provider's policy for
# this whole title, and every probe after that is known-dead time. Measured 2026-09-16: 154 x 451
# and 67 x 429 over ten plays, 157 s in Real-Debrid alone.

# Three is `probeCount`'s own default and what the data supports: when Real-Debrid blocks this
# library it blocks everything (20 of 20 ranked releases in six of those ten plays, and an unbroken
# run in all ten), so releases 4-20 carry nothing releases 1-3 did not.
CONSECUTIVE_BLOCK_CAP = 3
# After the remembered verdict expires the provider is on probation: **one** probe, not three. A
# renewed plan recovers on the first call; a still-blocked provider re-arms after one wasted 150 ms.
PROBATION_BLOCK_CAP = 1
# ...and probation itself is forgotten, so a provider blocked once yesterday gets the full three
# strikes again today rather than living on one forever.
PROBATION_FORGET_S = 24 * 3600
# The ceiling from `core.SETTING_BOUNDS["providerBlockMinutes"]`, restated as the guard's own
# refusal: an entry claiming a longer life than this is not honoured at all. A hand-edited (or
# corrupted) cache file must not be able to disable a provider permanently.
BLOCK_TTL_CEILING_S = 120 * 60

# The providers' names as a person writes them. Here rather than in `cli.py` because the guard's own
# log lines need them too, and one vocabulary is the point.
PROVIDER_NAMES = {"torbox": "TorBox", "realdebrid": "Real-Debrid"}


def handed_over(log: Log, stage: str, files: list[dict], picked: dict, **where) -> dict | None:
    """The provider's file that `pick_pack_files` picked (`cand["file"]`). By its full path
    first, since a pack may hold `Season 1/Show - 07.mkv` and `Season 2/Show - 07.mkv`; by its basename only when exactly
    one file ends with it. None otherwise: the provider then picks again, with the entry's identity."""
    name, base = str(picked.get("name") or ""), str(picked.get("basename") or "")

    def full(f):
        return str(f.get("name") or f.get("path") or "")
    by_path = [f for f in files if name and (full(f) == name or full(f).endswith("/" + name)
                                             or name.endswith("/" + full(f)))]
    by_base = [f for f in files if base and (full(f) == base or full(f).endswith("/" + base))]
    how = "path" if len(by_path) == 1 else "basename" if len(by_base) == 1 else None
    log.info(stage, "handover", result="PASS" if how else "SKIP", file=name or base, by=how, **where,
             matches={"path": len(by_path), "basename": len(by_base)},
             reason=("the file the pack's pick chose, found by its " + how) if how else
                    "the picked file is not one unique file here; picking again with the entry's identity")
    return (by_path if how == "path" else by_base)[0] if how else None


def identity_for(log: Log, stage: str, cand: dict) -> dict | None:
    """The entry identity `_resolve` put on the candidate (`cand["entry"]`), for a provider's own file pick.
    A candidate without one keeps the number-only rule, and says so."""
    entry = cand.get("entry")
    if entry is None:
        log.info(stage, "identity", result="SKIP", id=cand.get("id"),
                 reason="no entry identity on this candidate: its file is picked by its number alone")
    return entry


FIRST_SEASON_CODES = (FILE_FRANCHISE_SHOW, FILE_OTHER_SEASON, FILE_EXTRAS_FOLDER)


def log_first_season_pick(log: Log, site: str, cand: dict, picked: dict | None, skipped: list[dict]) -> None:
    """One `score.file_identity pick` line for a first season's pack whose file the first-season rule chose
    (`score.first_season_file_check`), wherever it was picked (`site`: `score.file_pick`, `rd.filepick`,
    `torbox.filepick`): the rule, the file, the files set aside with their reason, and the franchise source. Nothing
    for a pick the rule did not read."""
    aside = [{"path": x["path"], "code": x["code"], "against": x["against"]} for x in skipped
             if x.get("code") in FIRST_SEASON_CODES]
    ident = (picked or {}).get("identity") or {}
    if ident.get("rule") != "first_season" and not aside:
        return
    entry = cand.get("entry") or {}
    log.info("score.file_identity", "pick", result="PASS" if picked else "FAIL", site=site, id=cand.get("id"),
             title=cand.get("title"), rule="first_season", file=(picked or {}).get("basename"),
             left=ident.get("left", 0), preferred=ident.get("preferred", False), root=ident.get("root"),
             set_aside=aside[:10],
             set_aside_more=max(0, len(aside) - 10), franchise=entry.get("first_season_from"),
             shows=len({s[0] for s in entry.get("first_season_shows") or ()}),
             reason=("a first season's pack naming more than the entry: the file is the entry's, after the files "
                     "of another show, season or extras folder" + ("; a file naming nothing more was preferred"
                                                                     if ident.get("preferred") else "")
                     if picked else "every file numbered like this episode is another show's, season's or an "
                                    "extras folder's: the pack has no file of the entry for it"))


def block_file() -> Path:
    """Resolved at call time, not bound as a default: the tests point `providers.CACHE_DIR` at a tmpdir."""
    return CACHE_DIR / "provider-blocks.json"


class BlockGuard:
    """One resolve's memory of which providers have refused, plus a short-lived on-disk verdict.

    Graduated, never a kill switch: warn at cap-1, stop probing *that* provider at cap,
    fall through to the next one in `providers`, and forget it again on any success or after
    `providerBlockMinutes`. Nothing here can abandon a resolve or touch another provider's budget.
    """

    def __init__(self, log: Log, settings: dict, path: Path | None = None):
        self.log, self.settings = log, settings
        self._path = path
        self.counts: dict[str, int] = {}
        self.cost_ms: dict[str, float] = {}
        self.stopped: dict[str, dict] = {}      # provider -> the note for `probe_errors`
        # `record_ok` runs on every non-blocked probe, so it must not re-read the file each time
        # once it has seen there is nothing to clear.
        self._clear_done: set[str] = set()
        self._probation: dict[str, bool] = {}
        self._said_expiry: set[str] = set()
        # One verdict per provider per resolve: `remembered()` is asked twice for TorBox (once to
        # decide whether `checkcached` is worth a second, once in the provider loop) and the trace
        # should carry one line, not two.
        self._verdict: dict[str, dict | None] = {}

    # ------------------------------------------------------------------ state

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else block_file()

    @property
    def ttl_seconds(self) -> float:
        from .core import as_int, DEFAULT_SETTINGS
        minutes = as_int(self.settings.get("providerBlockMinutes"), DEFAULT_SETTINGS["providerBlockMinutes"])
        # Clamped here too: `coerce_settings` is the front door, but a caller that built its own
        # settings dict must not be able to hand the guard an unbounded lifetime.
        return max(60.0, min(float(minutes) * 60.0, float(BLOCK_TTL_CEILING_S)))

    def _read(self) -> dict:
        data = cached_json(self.path, None)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _live(entry, now: float) -> dict | None:
        """The stored verdict, only if every one of the three permanence guards agrees."""
        if not isinstance(entry, dict):
            return None
        try:
            until, at = float(entry.get("until") or 0), float(entry.get("at") or 0)
        except (TypeError, ValueError):
            return None
        if until <= now:                                # expired: the provider is probed again
            return None
        if until - at > BLOCK_TTL_CEILING_S:            # hand-edited or corrupt: refuse to honour it
            return None
        if at > now + 60:                               # stamped in the future (clock moved): same
            return None
        return entry

    @staticmethod
    def releases(n: int) -> str:
        """"1 release", not "1 releases": the probation path probes exactly one."""
        return f"{n} release" + ("" if n == 1 else "s")

    def cap(self, provider: str) -> int:
        """How many consecutive blocks this provider may answer before it is skipped."""
        return PROBATION_BLOCK_CAP if self._probation.get(provider) else CONSECUTIVE_BLOCK_CAP

    # ------------------------------------------------------------------ decisions

    def remembered(self, provider: str, now: float | None = None) -> dict | None:
        """The live verdict for `provider`, or None — and a probation flag when one has just expired.

        Logged, not silent: a provider that is skipped without a line in the trace is skipped
        invisibly.
        """
        if provider in self._verdict:
            return self._verdict[provider]
        now = time.time() if now is None else now
        entry = (self._read() or {}).get(provider)
        live = self._live(entry, now)
        self._verdict[provider] = live
        if live is not None:
            left = round(float(live["until"]) - now)
            self.log.info("provider.block", "skip", result="SKIP", provider=provider,
                          consecutive=live.get("consecutive"), blocked_stage=live.get("stage"),
                          error=live.get("error"), since=live.get("at"), expires_in_s=left,
                          first_seen_trace=live.get("trace"),
                          suggest=f"{PROVIDER_NAMES.get(provider, provider)} refused "
                                  f"{self.releases(int(live.get('consecutive') or 0))} in a row "
                                  f"{round(now - float(live.get('at') or now))} s "
                                  f"ago, so it is not asked for another {left} s; any success clears it, "
                                  f"or set providerBlockMinutes lower")
            return live
        if isinstance(entry, dict):
            try:
                age = now - float(entry.get("at") or 0)
            except (TypeError, ValueError):
                age = PROBATION_FORGET_S + 1
            if 0 <= age <= PROBATION_FORGET_S:
                self._probation[provider] = True
                if provider not in self._said_expiry:
                    self._said_expiry.add(provider)
                    self.log.info("provider.block", "probation", result="PASS", provider=provider,
                                  blocked_ago_s=round(age), cap=PROBATION_BLOCK_CAP,
                                  suggest=f"{PROVIDER_NAMES.get(provider, provider)}'s block expired; "
                                          f"one probe decides whether it comes back")
        return None

    def record_ok(self, provider: str) -> None:
        """Any outcome that is not a block: the run is broken, and a working provider is not blocked."""
        had = self.counts.get(provider) or 0
        self.counts[provider] = 0
        self._probation.pop(provider, None)
        if provider in self._clear_done:
            return
        self._clear_done.add(provider)
        store = self._read()
        if provider in store:
            store.pop(provider, None)
            write_cache(self.path, store)
            self.log.info("provider.block", "cleared", result="PASS", provider=provider,
                          reason="the provider answered something other than a block")
        elif had:
            self.log.debug("provider.block", "reset", result="PASS", provider=provider, was=had)

    def record_block(self, provider: str, exc: StageError, ms: float = 0.0, now: float | None = None) -> bool:
        """One blocked release. Returns True when this is the one that trips the cap."""
        now = time.time() if now is None else now
        n = (self.counts.get(provider) or 0) + 1
        self.counts[provider] = n
        self.cost_ms[provider] = (self.cost_ms.get(provider) or 0.0) + float(ms or 0.0)
        cap = self.cap(provider)
        if n < cap:
            if n == cap - 1:
                # Warn before refusing, and say what the next one costs.
                # `blocked_stage`, not `stage`: a data key called `stage` binds to `Log.emit`'s own
                # parameter and raises a TypeError on the play path (every `play.failed` event was once lost this way).
                self.log.warn("provider.block", "warn", result="PASS", provider=provider, consecutive=n,
                              cap=cap, blocked_stage=exc.stage, error=str(exc), ms=round(self.cost_ms[provider], 1),
                              suggest=f"one more block and {PROVIDER_NAMES.get(provider, provider)} is "
                                      f"skipped for the rest of this play")
            return False
        ttl = self.ttl_seconds
        entry = {"provider": provider, "consecutive": n, "stage": exc.stage, "error": str(exc),
                 "suggest": exc.suggest, "at": now, "until": now + ttl, "ttl_s": round(ttl),
                 "ms": round(self.cost_ms[provider], 1), "trace": getattr(self.log, "trace", "")}
        store = self._read()
        store[provider] = entry
        write_cache(self.path, store)
        self.stopped[provider] = entry
        self.log.warn("provider.block", "cap", result="FAIL", provider=provider, consecutive=n, cap=cap,
                      blocked_stage=exc.stage, error=str(exc), ms=round(self.cost_ms[provider], 1),
                      remembered_for_s=round(ttl), probation=bool(self._probation.get(provider)),
                      suggest=f"{PROVIDER_NAMES.get(provider, provider)} refused {self.releases(n)} in a row for the "
                              f"same reason; skipping it for the rest of this play and for {round(ttl / 60)} min. "
                              f"{exc.suggest or 'the next provider in `providers` is tried instead'}")
        return True

    def note(self, provider: str, remembered: dict | None = None, skipped: int = 0) -> dict:
        """The `probe_errors` row, so the CLI's own failure summary says why a provider stopped."""
        entry = remembered or self.stopped.get(provider) or {}
        n = entry.get("consecutive") or self.counts.get(provider) or 0
        if remembered is not None:
            ago = max(0, round(time.time() - float(entry.get("at") or 0)))
            error = f"not asked: refused {self.releases(n)} in a row {ago} s ago ({entry.get('error')})"
        else:
            error = f"blocked {self.releases(n)} in a row ({entry.get('error')}); not asked again this play"
        row = {"provider": provider, "stage": "provider.block", "error": error,
               "consecutive": n, "suggest": entry.get("suggest") or ""}
        if skipped:
            row["releases_skipped"] = skipped
        return row


# ----------------------------------------------------------------------------- Real-Debrid

# Map Real-Debrid's numeric error codes to something the user can act on. The codes and
# the torrent-status vocabulary below were read from the official reference on 2026-09-15
# (`curl -sL https://api.real-debrid.com/`), not guessed.
RD_ERRORS = {
    8: ("Real-Debrid rejected the token (bad token)", "re-create the token at real-debrid.com/apitoken"),
    9: ("Real-Debrid says permission denied — the account is not premium, or it is locked",
        "renew premium at real-debrid.com, or remove Real-Debrid from the providers setting"),
    14: ("Real-Debrid says the account is locked", "check real-debrid.com/account"),
    15: ("Real-Debrid says the account is not activated", "activate it at real-debrid.com"),
    21: ("Real-Debrid has too many active downloads on this account",
         "wait for one to finish, or delete a torrent at real-debrid.com/torrents"),
    34: ("Real-Debrid rate limit (250/min) hit", "wait a minute"),
    36: ("Real-Debrid fair-usage limit reached", "wait for the quota to reset, or use TorBox"),
    37: ("Real-Debrid has disabled this endpoint", "nothing to do on this side; use TorBox"),
}

# The codes that mean *the account*, not this release and not this moment. They answer the
# same way for every release, so the block guard counts them and the provider is stepped over once
# the run is long enough to be conclusive. 34 (rate limit) and 36 (fair usage) are deliberately not
# here: they are transient, they must *reset* the counter rather than trip it, and 429 already backs
# off inside `Http.request`.
RD_ACCOUNT_CODES = {8, 9, 14, 15}

# Terminal torrent states. `dead` is the main one; the rest end the same way and deserve
# their own words rather than "torrent ended in status X".
RD_DEAD_STATES = {
    "dead": ("this release is dead on Real-Debrid (no seeders left)", "try the next release"),
    "magnet_error": ("Real-Debrid could not convert the magnet for this release", "try the next release"),
    "error": ("Real-Debrid reported an error for this release", "try the next release"),
    "virus": ("Real-Debrid flagged this release as a virus", "try the next release"),
}


class RealDebrid:
    """Verified against https://api.real-debrid.com/ docs (2026-09-14)."""

    def __init__(self, http: Http, log: Log, token: str):
        self.http, self.log = http, log
        self.headers = {"Authorization": f"Bearer {token}"}

    def _call(self, stage: str, method: str, path: str, form: dict | None = None):
        data = urllib.parse.urlencode(form).encode() if form else None
        headers = dict(self.headers)
        if data:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        status, body, hdrs = self.http.request(f"rd.{stage}", method, f"{RD_URL}{path}", headers=headers, data=data)
        code = body.get("error_code") if isinstance(body, dict) else None
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
        if code in RD_ERRORS:
            message, suggest = RD_ERRORS[code]
            kind = BlockedError if code in RD_ACCOUNT_CODES else StageError
            raise kind(f"rd.{stage}", f"{message} (error {code})", suggest, error_code=code)
        if status == 401:
            raise BlockedError(f"rd.{stage}", "Real-Debrid rejected the token (401)", "re-create the token at real-debrid.com/apitoken")
        if status == 403:
            raise BlockedError(f"rd.{stage}", "Real-Debrid says the account is not premium or is locked (403)", "renew premium")
        if status == 429:
            raise StageError(f"rd.{stage}", "Real-Debrid rate limit (250/min) hit", "wait a minute")
        if status >= 400 and isinstance(body, dict) and body.get("error") and status != 451:
            raise StageError(f"rd.{stage}", f"Real-Debrid: {body.get('error')} ({status})",
                             "see real-debrid.com or try the next release", error_code=code, status=status)
        return status, body

    def user(self) -> dict:
        _, body = self._call("user", "GET", "/user")
        return body

    def add_magnet(self, info_hash: str) -> str:
        status, body = self._call("addMagnet", "POST", "/torrents/addMagnet", {"magnet": f"magnet:?xt=urn:btih:{info_hash}"})
        if status == 451 or (isinstance(body, dict) and body.get("error") == "infringing_file"):
            # Real-Debrid's keyword filter. Not a cache miss; do not spend the probe budget on it.
            raise BlockedError("rd.addMagnet", "Real-Debrid blocks this release (451 infringing_file)",
                               "try TorBox", info_hash=info_hash)
        if status != 201 or not isinstance(body, dict) or "id" not in body:
            raise StageError("rd.addMagnet", f"addMagnet returned {status}: {body}", "check the infohash / RD status", body=body)
        self.log.info("rd.addMagnet", "done", result="PASS", torrent_id=body["id"], info_hash=info_hash)
        return body["id"]

    def info(self, torrent_id: str) -> dict:
        status, body = self._call("info", "GET", f"/torrents/info/{torrent_id}")
        if status != 200 or not isinstance(body, dict):
            raise StageError("rd.info", f"info returned {status}: {body}", body=body)
        return body

    def select_files(self, torrent_id: str, files: str = "all") -> None:
        status, body = self._call("selectFiles", "POST", f"/torrents/selectFiles/{torrent_id}", {"files": files})
        if status not in (202, 204):
            raise StageError("rd.selectFiles", f"selectFiles returned {status}: {body}", body=body)
        self.log.info("rd.selectFiles", "done", result="PASS", torrent_id=torrent_id, files=files, status=status)

    def unrestrict(self, link: str) -> dict:
        status, body = self._call("unrestrict", "POST", "/unrestrict/link", {"link": link})
        if status != 200 or not isinstance(body, dict) or "download" not in body:
            raise StageError("rd.unrestrict", f"unrestrict returned {status}: {body}", body=body)
        self.log.info("rd.unrestrict", "done", result="PASS", filename=body.get("filename"), filesize=body.get("filesize"),
                      streamable=body.get("streamable"), host=link_host(body["download"]))
        return body

    def delete(self, torrent_id: str) -> None:
        status, _ = self._call("delete", "DELETE", f"/torrents/delete/{torrent_id}")
        self.log.info("rd.delete", "done", result="PASS" if status == 204 else "FAIL", torrent_id=torrent_id, status=status)

    def resolve(self, cand: dict, poll_seconds: int) -> dict:
        """Probe one candidate. Returns {cached, download, filename, torrent_id, status, progress}."""
        t0 = time.monotonic()
        torrent_id = self.add_magnet(cand["info_hash"])
        try:
            info = self.info(torrent_id)
            deadline = time.monotonic() + poll_seconds
            delay = 1.0
            while info.get("status") in ("magnet_conversion", "queued") and time.monotonic() < deadline:
                time.sleep(delay)
                delay = min(delay * 1.5, 4.0)
                info = self.info(torrent_id)
            if info.get("status") == "waiting_files_selection":
                selection = "all"
                rd_files = info.get("files") or []
                if len(rd_files) > 1 and cand.get("episode"):
                    skipped: list = []
                    core.log_names_cut(self.log, "rd.filepick", [str(f.get("path") or "") for f in rd_files],
                                       torrent_id=torrent_id)
                    core.log_sizes_refused(self.log, "rd.filepick", [f.get("bytes") for f in rd_files],
                                           torrent_id=torrent_id)
                    picked = pick_episode_file([{**f, "name": core.cut_file_name(str(f.get("path") or ""))}
                                                for f in rd_files], cand["episode"],
                                               cand.get("alt"), identity_for(self.log, "rd.filepick", cand),
                                               cand.get("title") or "", skipped)
                    log_first_season_pick(self.log, "rd.filepick", cand, picked, skipped)
                    if picked:
                        selection = str(picked["id"])
                        self.log.info("rd.filepick", "pick", result="PASS", torrent_id=torrent_id, file=picked["basename"],
                                      matched=picked["matched"], identity=picked.get("identity"),
                                      read=picked.get("read"),
                                      **skipped_summary(skipped, cand.get("entry")))
                    elif (cand.get("parsed") or {}).get("batch"):
                        aside = set_aside_words(skipped)
                        raise StageError("rd.filepick", f"no file for episode {cand['episode']}"
                                         + (f"; set aside: {aside}" if aside else ""),
                                         pick_refused_suggest(skipped), torrent_id=torrent_id,
                                         **skipped_summary(skipped, cand.get("entry")))
                    else:
                        # A single's torrent with no file read as the episode keeps
                        # every file, as before; say so, with what was passed over.
                        self.log.warn("rd.filepick", "fallback", result="SKIP", torrent_id=torrent_id,
                                      files=len(rd_files), **skipped_summary(skipped, cand.get("entry")),
                                      suggest="no file read as this episode; every file is selected (not a pack)")
                self.select_files(torrent_id, selection)
                info = self.info(torrent_id)
                while info.get("status") in ("magnet_conversion", "queued") and time.monotonic() < deadline:
                    time.sleep(1.0)
                    info = self.info(torrent_id)
            st = info.get("status")
            self.log.info("rd.info", "state", torrent_id=torrent_id, status=st, progress=info.get("progress"),
                          files=len(info.get("files") or []), links=len(info.get("links") or []),
                          ms=(time.monotonic() - t0) * 1000)
            if st == "downloaded" and info.get("links"):
                link = info["links"][0]
                unres = self.unrestrict(link)
                check_stream(self.log, "rd.stream", unres["download"], torrent_id=torrent_id)
                return {"cached": True, "download": unres["download"], "filename": unres.get("filename"),
                        "filesize": unres.get("filesize"), "torrent_id": torrent_id, "status": st, "progress": 100}
            if st in ("downloading", "queued", "magnet_conversion"):
                self.log.warn("rd.info", "uncached", result="FAIL", torrent_id=torrent_id, status=st,
                              progress=info.get("progress"), suggest="not cached on RD; try the next candidate or wait")
                return {"cached": False, "torrent_id": torrent_id, "status": st, "progress": info.get("progress")}
            message, suggest = RD_DEAD_STATES.get(str(st), (f"the torrent ended in status {st} on Real-Debrid",
                                                            "try the next release"))
            raise StageError("rd.info", message, suggest, torrent_id=torrent_id, status=st)
        except StageError:
            self.delete(torrent_id)
            raise


# Nothing the public plugin adds uploads, and that holds for the debrid path too.
# TorBox docs, `createtorrent` form field `seed` (Postman collection, read 2026-09-26): "1 is auto.
# 2 is seed. 3 is don't seed. Optional. Default is 1, or whatever the user has in their settings.
# Overwrites option in settings." `1` left it to the account's setting.
TORBOX_NO_SEED = 3
# The two `createtorrent` limits (same page): every add draws on the first; an add TorBox has not
# cached draws on the second as well. The log carries the docs' words; the error text says the same
# limit without the word "uncached", which `cli.diagnose_failure` reads as "the download carries on".
TORBOX_REQUESTS_PER_MINUTE = 300
TORBOX_UNCACHED_PER_HOUR = 60
TORBOX_REQUEST_LIMIT = f"{TORBOX_REQUESTS_PER_MINUTE} requests per minute"
TORBOX_UNCACHED_LIMIT = f"{TORBOX_UNCACHED_PER_HOUR} uncached torrents per hour"
TORBOX_LIMIT_WORDS = {
    TORBOX_REQUEST_LIMIT: (f"TorBox's limit of {TORBOX_REQUESTS_PER_MINUTE} adds per minute was reached (429)",
                           "wait a minute and play again"),
    TORBOX_UNCACHED_LIMIT: (f"TorBox's limit of {TORBOX_UNCACHED_PER_HOUR} adds per hour for torrents it has not "
                            f"cached yet was reached (429)",
                            "wait up to an hour before adding another release TorBox does not have yet, "
                            "or pick a release TorBox already has cached"),
}
# The wait for a torrent a play added in place of a cached dub, or with nothing cached (`TorBox.wait_ready`). The docs
# (`GET /torrents/mylist`, "Get Torrent List", collection read 2026-10-19) list `download_state`, `progress`, `size`,
# `total_downloaded`, `download_speed`, `eta`, `seeds`, `peers`, `download_finished` and `download_present` per
# torrent, and say of the state "completed": "Do not use this for download completion status", so ready is
# `download_finished` and `download_present`, as `_stream` reads it. Every endpoint but the adds is limited to
# "300/min per API token" (https://support.torbox.app/en/articles/13726368-api-rate-limits): a poll every 3 s is 20 a
# minute, 3,600 in the three hours of `KEEP_WAITING_MAX_S`. TorBox refreshes `total_downloaded` and `progress` only
# every 25-55 s (measured 2026-10-23 on three downloads; the trace `ui-play-1792707646-8709` agrees), so the numbers
# still move about every 30 s: the 3 s poll catches each change within 3 s of TorBox making it. The gap is counted
# from one poll's start to the next, so a slow answer does not stretch it. `playstate.ABANDON_AFTER_S` (20 s) is how
# long the popout gives a silent play before it reads it as gone: the status is written before and after every poll,
# failed or not, and every `WAIT_CAP_S` of a longer gap (a backoff), so the longest silence is 8 s of a nap or one
# poll: one attempt, its timeout `WAIT_POLL_TIMEOUT_S` (or the time left). That timeout bounds each socket operation
# (the connect, each read), not the whole request: an answer that trickles in, or a name lookup that stalls (it has
# no timeout), can take longer. The next poll is the retry, so a poll is never retried inside `Http`.
WAIT_POLL_S = 3.0
WAIT_CAP_S = 8.0
# A poll that failed backs off, from its end: the gap doubles with each failure in a row (6 s, 12 s, … up to
# `WAIT_BACKOFF_MAX_S`), drawn up to this share longer (never shorter: no gap is under `WAIT_POLL_S`), or TorBox's
# own `Retry-After` when it asks for longer (a 429 is its rate limit); the cap holds after the draw, so no gap is
# longer than `WAIT_BACKOFF_MAX_S`. The first poll that answers goes back to 3 s.
WAIT_JITTER = 0.25
WAIT_BACKOFF_MAX_S = 60.0
WAIT_POLL_TIMEOUT_S = 8.0
# Between polls the status is still written at least every `WAIT_CAP_S` (the popout reads 20 s of silence as a play
# gone), and the sleep runs in slices of this, so a viewer's "Keep waiting" during the wait (a signal whose handler
# only records it) is on screen within about a second rather than at the next poll.
WAIT_SLICE_S = 1.0
# The wait's only end besides TorBox finishing, the viewer and a stop: then it holds the choices (`core.Held`). Counted
# on the wall clock (`_wall_clock`), so a suspend counts toward the three hours.
KEEP_WAITING_MAX_S = 3 * 3600
# The clock, the sleep and the jitter's draw, as module names so a test runs a three-hour wait in no time. A monotonic
# clock (CLOCK_MONOTONIC here, `time.get_clock_info`) would leave suspended time out of the cap.
_wall_clock, _wait_sleep, _wait_random = time.time, time.sleep, random.random


def poll_gap(failures: int = 0, retry_after=None, draw: float = 0.0) -> tuple[float, str]:
    """The gap to the next poll, and why: `WAIT_POLL_S` while polls answer; after `failures` in a row, doubled for
    each (6 s, 12 s, …), or TorBox's `Retry-After` (seconds) when that is longer, then drawn up to `WAIT_JITTER`
    longer by `draw` (0-1, upward only), and capped at `WAIT_BACKOFF_MAX_S` after the draw. Never under
    `WAIT_POLL_S`."""
    if failures <= 0:
        return WAIT_POLL_S, f"every {WAIT_POLL_S:g} s while TorBox answers"
    gap = WAIT_POLL_S * 2 ** failures
    why = f"backing off after {failures} failed poll" + ("s" if failures != 1 else "") + " in a row"
    asked = retry_after_s(retry_after)
    if asked is not None and asked > gap:
        gap = float(asked)
        why += f"; TorBox asked for {asked} s"
    gap *= 1 + WAIT_JITTER * draw
    if gap >= WAIT_BACKOFF_MAX_S:
        gap = WAIT_BACKOFF_MAX_S
        why += f" (at most {WAIT_BACKOFF_MAX_S:g} s)"
    return gap, why


# The speed and time left are TorBox's byte counts over the wait's own clock (`ByteRate`), never its `eta`: the rate
# is the change in `total_downloaded` between the first sightings of two different counts about a minute apart: the
# newest, and the latest earlier count at least this far before it, else the oldest one there is. TorBox refreshes
# its counts 25-55 s apart, unevenly (the trace's 6 MB in 23 s after 63 MB in 54 s), so a pair a single refresh apart
# would swing the time left from 2 minutes to 10; one a minute or more apart spans two refreshes.
RATE_WINDOW_S = 60.0
# A count that has not changed for this long has missed TorBox's refresh at least once (25-55 s measured, up to about
# 63 s seen in the trace's coarser polls), so the rate is no longer known: the line drops the speed and the time
# left, and keeps the percentage. No count older than this pairs with the newest either.
RATE_STALE_S = 90.0
# Below this TorBox's own `download_speed` means the download is not moving: it answered 5 B/s, 169 B/s and 1,041 B/s
# in the ep 10 trace while it had lost its seed, and 372 KB/s or more whenever bytes came in (ep 10's slowest moving
# answer; probe3's were 6.4 MB/s or more). 10 KB/s sits between the two, so the guard needs no `seeds` (a swarm of
# peers with no full seed downloads too). TorBox's speed under it is not kept as a speed either.
SPEED_FLOOR_BPS = 10_000.0
# `say.speed_words` says no speed under this; a rate under it is not kept as a speed, so `speed_shown` is the line's.
SPEED_SHOWN_MIN_BPS = 1000.0
# A count lower than the last one is refused once (a glitch); seen on this many polls in a row it is TorBox's count
# now (a download it checked again or started over), so the stretch starts over from it. TorBox repeats a count for
# 25-55 s, so a real restart is two polls in a row within 3 s; one stray answer is not.
RESTART_AFTER_LOWER = 2


class ByteRate:
    """The download's rate from TorBox's byte counts (`total_downloaded`), over one stretch of downloading. Each count
    is kept once, at the moment the wait first saw it (`points`, oldest first): TorBox repeats a count for 25-55 s
    between its refreshes, so a repeat adds no time, and the rate never divides by the time a count stood still. Only
    `downloading` readings with bytes count, and any other state (`stalled`, `checking`, …) ends the stretch: the
    next download starts a new one, so the time TorBox spent not downloading never lowers the rate. A count that goes
    backwards, or past the size when the size is known (TorBox answers `size` 0 at `metaDL` and -1 at `checking`), is
    refused (`observe`'s `refused`) and changes nothing; a lower count on `RESTART_AFTER_LOWER` polls in a row starts
    the stretch over from it (`restarted`).

    While TorBox says `downloading` but has lost its seed (its own `download_speed` under `SPEED_FLOOR_BPS`), the rate
    is a minute old and the download is not moving: the speed and the time left are not given (the percentage stays),
    and the stretch ends there, as a stall ends it: the next moving count starts over, with TorBox's own speed first.
    Kept, the stretch divided the minute TorBox spent not downloading into the bytes after it: on the ep 10 trace "82 %,
    167 KB/s, about 7 minutes left" for a remainder TorBox fetched in about 60 s at 1.2 MB/s. On that trace the guard
    covers three stretches of 45-63 s that read "under a minute left" or "about 25 minutes left" while nothing
    downloaded."""

    def __init__(self):
        self.points: list[tuple[float, float]] = []
        self.lower = 0                       # lower counts refused in a row

    def observe(self, now: float, state, downloaded, size, speed, seeds=None) -> dict:
        """One poll's reading: `{rate, speed, left_s, since_change_s, refused, why}`. `rate` (bytes/s) needs two counts
        at most `RATE_STALE_S` apart and the newest at most that old; `speed` is that rate, or TorBox's own
        `download_speed` while no rate exists yet in this stretch (one count so far); `left_s` is the bytes left
        (`size` - the newest count) over the rate. All three only while TorBox says `downloading`, and `speed` and
        `left_s` only while TorBox's own speed is at least `SPEED_FLOOR_BPS`. `seeds` is only logged."""
        out = self._observe(now, state, downloaded, size, speed)
        if out["refused"]:
            # A count that does not fit says nothing about the speed, and the old rate beside its percentage would
            # be two answers at once: the percentage alone, until a count fits again.
            out.update(speed=None, left_s=None)
        theirs = _number(speed)
        lost = str(state or "").lower() == "downloading" and theirs is not None and theirs < SPEED_FLOOR_BPS
        if lost:
            # The stretch ends here: no count seen while the seed was lost may anchor the next rate.
            shown = out["speed"] is not None or out["left_s"] is not None
            ended, self.points = bool(self.points), []
            out.update(speed=None, left_s=None)
            if shown or ended:
                out["why"] = (f"{out['why']}; not shown: TorBox reports {theirs:g} B/s (seeds: {seeds}), under "
                              f"{SPEED_FLOOR_BPS:g} B/s, so the download is not moving; the stretch ends")
        return out

    def _observe(self, now: float, state, downloaded, size, speed) -> dict:
        downloading = str(state or "").lower() == "downloading"
        out = {"rate": None, "speed": None, "left_s": None, "since_change_s": None, "refused": None,
               "restarted": None}
        lower, self.lower = self.lower, 0
        if not downloading:
            ended = bool(self.points)
            self.points = []
            out["why"] = f"not downloading ({state})" + ("; the stretch of downloading ended" if ended else "")
            return out
        try:
            count = float(downloaded)
        except (TypeError, ValueError):
            count = None
        try:
            total = float(size)
        except (TypeError, ValueError):
            total = 0.0
        # An infinite count or size is no count (`int(inf)` in the lines below raised out of the wait).
        count = count if count is None or math.isfinite(count) else None
        total = total if math.isfinite(total) else 0.0
        if count is not None and count > 0:
            if total > 0 and count > total:
                out["refused"] = f"{int(count)} bytes downloaded is more than the size ({int(total)})"
            elif self.points and count < self.points[-1][1] and lower + 1 < RESTART_AFTER_LOWER:
                self.lower = lower + 1
                out["refused"] = f"{int(count)} bytes downloaded is less than the {int(self.points[-1][1])} seen before"
            elif self.points and count < self.points[-1][1]:
                out["restarted"] = (f"{int(count)} bytes downloaded, less than the {int(self.points[-1][1])} seen "
                                    f"before, on {lower + 1} polls in a row: TorBox's count is lower now (checked "
                                    f"again or started over), so the rate starts over from it")
                self.points = [(now, count)]
            elif not self.points or count != self.points[-1][1]:
                self.points.append((now, count))
                # Kept by their distance from the newest count, never from the clock: between two counts the rate
                # stays exactly what it was, however many repeats come in.
                self.points = [p for p in self.points if now - p[0] <= RATE_STALE_S]
        if not self.points:
            out.update(speed=_positive(speed), why="no byte count from this download yet: TorBox's own speed")
            return out
        newest_t, newest = self.points[-1]
        since = now - newest_t
        out["since_change_s"] = round(since, 1)
        if since > RATE_STALE_S:
            out["why"] = f"the byte count has not changed for {since:.0f} s: no rate"
            return out
        earlier = [p for p in self.points[:-1] if newest_t - p[0] <= RATE_STALE_S]
        if not earlier:
            out.update(speed=_positive(speed), why="one byte count so far: TorBox's own speed, no time left")
            return out
        far = [p for p in earlier if newest_t - p[0] >= RATE_WINDOW_S]
        anchor = far[-1] if far else earlier[0]
        rate = (newest - anchor[1]) / (newest_t - anchor[0])
        out.update(rate=round(rate, 1), speed=round(rate, 1) if rate >= SPEED_SHOWN_MIN_BPS else None,
                   why=f"{int(newest - anchor[1])} bytes in {newest_t - anchor[0]:.1f} s")
        if total > 0 and rate > 0:
            out["left_s"] = round(max(0.0, total - newest) / rate, 1)
        return out

    def newest_age(self, now: float) -> float | None:
        """Seconds since the newest byte count of this stretch was first seen, or None with no count (none yet, or the
        stretch ended: a stall, a lost seed)."""
        return now - self.points[-1][0] if self.points else None


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _percent(value) -> int:
    """TorBox's `progress` (0 to 1) as a whole percentage, 0 when it is not a finite number (`round()` of
    `Infinity` raised out of the play)."""
    try:
        number = float(value or 0) * 100
    except (TypeError, ValueError):
        return 0
    return round(number) if math.isfinite(number) else 0


def _positive(value) -> float | None:
    """TorBox's own speed, when it is one (at least `SPEED_FLOOR_BPS`), else None."""
    number = _number(value)
    return number if number is not None and number >= SPEED_FLOOR_BPS else None


class TorBoxSide(NamedTuple):
    """One TorBox error code that is TorBox's own side, not the release's and not the account's."""
    kind: str        # the probe row's `unavailable` value, which `say.torbox_side_words` reads
    message: str     # the error's message: what TorBox said, in the log's words
    suggest: str     # what to do; the probe row carries it, so `pin.apply unavailable` uses it as it is
    stops_adds: bool  # whether every later add TorBox has not cached is held back for the rest of the resolve
    over_403: bool = False  # whether a 403 with this code is this row, not an account block


# The TorBox error codes that are TorBox's side, one row each. The error codes table
# (collection, read 2026-09-27/28): "If the code ends in "ERROR", the error is the server's fault", and
# - NO_SERVERS_AVAILABLE_ERROR: "There are no download servers available to handle this request. This should
#   never happen. If you receieve this error, please contact us at contact@torbox.app." No status and no
#   Retry-After documented; live (2026-09-27) it came as 400 with `Retry-After: 3599` to an add without
#   `add_only_if_cached`, and a cached-only add sent seconds later answered 200. So it stops later uncached adds,
#   and it is classified on `createtorrent` only (the one step it was seen on).
# - DATABASE_ERROR: "Could not access internal database/memory store information." Documented on `requestdl`
#   (500) and `mylist`, never on `createtorrent`.
# - DOWNLOAD_SERVER_ERROR: "There was an error interacting with the download on the download server. It is
#   recommdned to simply wait some time before trying again." Documented on `createtorrent` (400, "Unknown
#   Error. Could not add to client. Please try again later.").
# Neither of the last two has been seen live. They are about one download, not TorBox's capacity, so later adds
# are still sent. (`torbox_torrent_info` reads DOWNLOAD_SERVER_ERROR as its own timeout: not here.)
# - AUTH_ERROR: "There was an error verifying the given authentication." Documented on `requestdl` as 500
#   ("There was an error verifying your API key. Please try again later.") and 403 ("An error occurred while
#   verifying your token. Please try again."), and on `mylist` as 500. A bad token is another code on the same
#   endpoints (403 `BAD_TOKEN`, 403 `NO_AUTH`), so this is TorBox failing to check the account: `over_403`, and
#   `_call` lets its 403 through to this table instead of the account block. Not seen live; stops nothing.
TORBOX_NO_SERVERS = "NO_SERVERS_AVAILABLE_ERROR"
TORBOX_WAIT = "TorBox's side, not this release: TorBox asks to wait some time before trying again"
TORBOX_SIDE = {
    TORBOX_NO_SERVERS: TorBoxSide(
        "no_servers", "TorBox has no download server free to start this download (NO_SERVERS_AVAILABLE_ERROR)",
        "TorBox's side, not this release: try again later; if it keeps happening, TorBox asks to be told at "
        "contact@torbox.app", True),
    "DATABASE_ERROR": TorBoxSide(
        "database", "TorBox could not reach its own database (DATABASE_ERROR)", TORBOX_WAIT, False),
    "DOWNLOAD_SERVER_ERROR": TorBoxSide(
        "download_server", "TorBox's download server failed on this download (DOWNLOAD_SERVER_ERROR)",
        TORBOX_WAIT, False),
    "AUTH_ERROR": TorBoxSide(
        "auth", "TorBox could not verify the account on its side (AUTH_ERROR)",
        "TorBox's side, not the token and not this release: TorBox asks to try again later",
        stops_adds=False, over_403=True),
}
# "UNKNOWN_ERROR — The reason for the error is unknown. Usually there will be error data attached in the
# "data" key." It stays a generic failure; its `data` is logged (bounded) so a report to TorBox has it.
TORBOX_UNKNOWN = "UNKNOWN_ERROR"
TORBOX_DATA_LOG_CHARS = 300


def header(headers: dict | None, name: str) -> str | None:
    """One response header by name, case-insensitively. `Http.request` hands back a plain dict, which
    loses `HTTPMessage`'s case-insensitive lookup, and TorBox sends `retry-after` in lower case."""
    want = name.lower()
    return next((str(v) for k, v in (headers or {}).items() if str(k).lower() == want), None)


def retry_after_s(value) -> int | None:
    """`Retry-After` in seconds when it is given as seconds (ASCII digits); None otherwise (an HTTP-date,
    missing, garbage). Never raises: it runs inside `_add`'s error handling, where anything but a
    `StageError` would end the whole resolve. `isdecimal`, not `isdigit`: "²" is a digit that `int` refuses."""
    text = str(value or "").strip()
    if not (text.isascii() and text.isdecimal()):
        return None
    try:
        return int(text)
    except ValueError:
        return None


# ----------------------------------------------------------------------------- torrent info
#
# `GET /torrents/torrentinfo?hash=&timeout=&use_cache_lookup=` (Postman collection, read 2026-09-26): "TorBox
# will return data about the torrent. This data is retrieved from the Bittorrent network [...] If the request
# goes longer than 10 seconds, TorBox will cancel it. You can adjust this [...] This route is cached as well"
# and "Authorization: None required." `use_cache_lookup`: "If you want to use a pre-cached request if sent
# before [...] Default false." Success is `{"success": true, "data": {"name", "hash", "size", "trackers",
# "seeds", "peers", "files": [{"name", "size"}]}}`; a timeout is HTTP 500 `DOWNLOAD_SERVER_ERROR` ("Could not
# download full metadata for the torrent within the alloted timeout."). Checked live without a token on a
# public sample hash: 200 in 0.33 s with `files` (each also carrying an `id`); an unknown hash at `timeout=3`
# answered that 500 after 3.36 s, twice (a failure is not cached). Nothing is added to any account.
TORRENT_INFO_PATH = "/torrents/torrentinfo"
# TorBox answers its own timeout 0.4-1.2 s after `timeout` (measured 2026-09-27: 5.5-6.2 s at `timeout=5`, one to
# four lookups side by side), once 9.2 s. The socket waits this much longer, then the pack stays unknown: the
# viewer's `torrentInfoSeconds` bounds the wait, not TorBox's load.
TORRENT_INFO_SLACK_S = 2.0


def torbox_torrent_info(http: Http, log: Log, info_hash: str, timeout_s: int) -> dict:
    """One release's files from TorBox's torrent info, without adding it anywhere and without a token.
    `{"status": FILES_COMPLETE, "files": [{name, size}]}`, or `FILES_UNKNOWN` (TorBox could not tell, or the
    hash is malformed) / `FILES_FAILED` (no answer) with `files` None; every
    lookup logs `torbox.torrentinfo lookup` with the hash, ms, file count and result. No retries: a 500 here
    is TorBox's own timeout, and three of them would triple the wait for an answer that is still unknown."""
    h = str(info_hash or "").lower()
    if len(h) != 40 or any(c not in "0123456789abcdef" for c in h):
        log.info("torbox.torrentinfo", "lookup", result="SKIP", hash=h[:40], reason="not a 40-hex infohash")
        return {"status": FILES_UNKNOWN, "files": None}
    url = f"{TORBOX_URL}{TORRENT_INFO_PATH}?" + urllib.parse.urlencode(
        {"hash": h, "timeout": int(timeout_s), "use_cache_lookup": "true"})
    t0 = time.monotonic()
    try:
        status, body, _ = http.request("torbox.torrentinfo", "GET", url, retries=1,
                                       timeout=timeout_s + TORRENT_INFO_SLACK_S)
    except StageError as exc:
        log.warn("torbox.torrentinfo", "lookup", result="FAIL", hash=h, ms=(time.monotonic() - t0) * 1000,
                 files=None, timeout_s=timeout_s, error=str(exc),
                 suggest="the pack's file list stays unknown; the provider lists it when it adds the torrent")
        return {"status": FILES_FAILED, "files": None}
    ms = (time.monotonic() - t0) * 1000
    data = body.get("data") if isinstance(body, dict) else None
    raw = data.get("files") if isinstance(data, dict) else None
    files = pack_files(raw, log, "torbox.torrentinfo", hash=h)
    if status == 200 and isinstance(body, dict) and body.get("success") is True and files:
        log.info("torbox.torrentinfo", "lookup", result="PASS", hash=h, ms=ms, files=len(files),
                 timeout_s=timeout_s, detail=body.get("detail"))
        return {"status": FILES_COMPLETE, "files": files}
    code = body.get("error") if isinstance(body, dict) else None
    log.warn("torbox.torrentinfo", "lookup", result="UNKNOWN", hash=h, ms=ms, files=None, status=status,
             error=code, timeout_s=timeout_s,
             suggest="TorBox could not read the torrent's metadata in time (few or no peers); the pack's file "
                     "list stays unknown until a provider adds it" if code == "DOWNLOAD_SERVER_ERROR"
             else "the pack's file list stays unknown; the provider lists it when it adds the torrent")
    return {"status": FILES_UNKNOWN, "files": None}


def torbox_usable(settings: dict) -> bool:
    """TorBox is one of the viewer's providers and has a token file (the token itself is not read). The
    one test for "may this play talk to TorBox at all", before the block guard has its say."""
    return "torbox" in (settings.get("providers") or []) and (core.CONFIG_DIR / "torbox-token").exists()


# `plan` on `GET /v1/api/user/me` is a number, and the Accounts row printed it as one
# ("1 · renews …"). TorBox's API reference documents the four values on that endpoint ("Plans": `0` is Free plan, `1`
# is Essential plan ($3 plan), `2` is Pro plan ($10 plan), `3` is Standard plan ($5 plan)); read 2026-10-18 from the
# collection behind https://api-docs.torbox.app (documenter.gw.postman.com/api/collections/29572726/2s9YXo1zX4).
# Any other value has no name here, and no name is shown rather than a number.
TORBOX_PLANS = {0: "Free", 1: "Essential", 2: "Pro", 3: "Standard"}


def torbox_plan_name(plan) -> str | None:
    """The name of TorBox's `plan` number, or None when it is not one TorBox documents."""
    if isinstance(plan, bool) or not isinstance(plan, int):
        return None
    return TORBOX_PLANS.get(plan)


class TorBox:
    """Verified live 2026-09-15 against api.torbox.app v1:
    createtorrent on a cached hash answers in ~0.9 s with torrent_id; mylist?id lists files with integer ids;
    requestdl returns a CDN URL that honours HTTP Range (206). checkcached takes a comma list of hashes."""

    def __init__(self, http: Http, log: Log, token: str):
        self.http, self.log, self.token = http, log, token
        self.headers = {"Authorization": f"Bearer {token}"}
        # The hashes this client's own `checkcached` answered as cached. One client lives for
        # one resolve, so this is exactly "checkcached said cached, this play" — never a flag
        # carried over from a stored search.
        self.cached_hashes: set[str] = set()
        # The limit a 429 on an uncached add named (a key of TORBOX_LIMIT_WORDS), else None.
        # The hourly budget does not come back within one play, so the rest of this play's uncached
        # adds are not sent (cached-only adds still are).
        self.uncached_limited: str | None = None
        # TorBox's own NO_SERVERS_AVAILABLE_ERROR answer to an add ({error_code, detail, status,
        # retry_after, info_hash}), else None. Every further add without `add_only_if_cached` in this
        # resolve would get the same answer, so none is sent; cached-only adds still are (live:
        # one answered 200 seconds after the refusal). Not persisted: the next resolve asks again.
        self.unavailable: dict | None = None
        # The error on TorBox's own side (`_side_error`'s shape) that a failed search names when it made
        # no TorBox add of its own: NO_SERVERS always, else the first DATABASE_ERROR / DOWNLOAD_SERVER_ERROR.
        # Only the diagnosis reads it; nothing is held back. ("Refusal" is auto-play's word.)
        self.side_error: dict | None = None
        # Every download this client started or found on the account for a release TorBox had not cached
        # ({torrent_id, info_hash, id, reused}), in order: an uncached add TorBox accepted (it answered a torrent
        # id), or the account's own entry `reuse_listed` used. Recorded before the read that follows, so a play that
        # allows one download knows it has one even when that read fails.
        self.downloads: list[dict] = []
        # The account's own list (`listed`), read once per client (one play: the pinned attempt and the search past it
        # share it) and every hash looked up in that one answer; the error, when that one read failed, is re-raised
        # for each later lookup instead of reading again. A download an earlier play left on the account is found
        # whatever this play's adds were answered (no server free, the hourly limit, a refused account).
        self.account: list | None = None
        self.account_error: StageError | None = None

    def _call(self, stage: str, method: str, path: str, *, form: dict | None = None, query: str = "",
              **http_kw):
        data = None
        headers = dict(self.headers)
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        url = f"{TORBOX_URL}{path}{('?' + query) if query else ''}"
        status, body, answered = self.http.request(f"torbox.{stage}", method, url, headers=headers, data=data,
                                                   log_url=url.replace(self.token, "…"), **http_kw)
        failed = isinstance(body, dict) and body.get("success") is False
        side = TORBOX_SIDE.get(str(body.get("error") or "")) if failed else None
        if status == 401 or (status == 403 and not (side and side.over_403)):
            # 401 and 403 are different answers and used to share one message that blamed
            # the token. Verified live 2026-09-16: an expired trial answers 200 on `/user/me` and
            # 403 `PLAN_RESTRICTED_FEATURE` on `createtorrent` — the token is fine, the plan is not,
            # and the old text sent the user to check the one thing that was right.
            # A 403 `AUTH_ERROR` (with `success: false`) is TorBox failing to check the account, not a
            # refusal of it; it goes on to the `TORBOX_SIDE` classifier with the other `success: false` answers.
            detail = ""
            if isinstance(body, dict):
                detail = str(body.get("detail") or body.get("error") or "").strip()
            if status == 403:
                message = f"TorBox refused this account ({status})" + (f": {detail}" if detail else "")
                suggest = "upgrade or renew the plan at torbox.app, or remove torbox from the providers setting"
            else:
                message = f"TorBox rejected the token ({status})" + (f": {detail}" if detail else "")
                suggest = "check ~/.config/omaanime/torbox-token, or sign in again: omarchy-anime auth torbox --device"
            raise BlockedError(f"torbox.{stage}", message, suggest, status=status)
        if status == 429:
            raise StageError(f"torbox.{stage}", "TorBox rate limit hit", "wait a minute", status=status, body=body,
                             retry_after=header(answered, "Retry-After"))
        if not isinstance(body, dict):
            raise StageError(f"torbox.{stage}", f"unexpected TorBox response ({status}): {str(body)[:200]}")
        if body.get("success") is False:
            if body.get("error") == TORBOX_UNKNOWN:
                # The docs put UNKNOWN_ERROR's detail in `data`; `Http` logs only a 4xx/5xx body.
                data = body.get("data")
                shown = None if data is None else json.dumps(data, ensure_ascii=False,
                                                             default=str)[:TORBOX_DATA_LOG_CHARS]
                self.log.warn(f"torbox.{stage}", "unknown_error", result="FAIL", status=status,
                              detail=body.get("detail"), data=shown,
                              reason="TorBox does not know the cause; the docs say its detail is in `data`",
                              suggest="if it keeps happening, report the request (with `data`) to contact@torbox.app")
            # The status and `Retry-After` ride along, so a caller that classifies the error code
            # can say when TorBox asked to be tried again. An `over_403` row (AUTH_ERROR) carries the
            # table's own words, so outside the play (sign-in check, `torbox-user`) it reads as TorBox's side,
            # not a raw code; inside it `resolve` classifies it from `body` as for every other row.
            if side and side.over_403:
                message, suggest = side.message, side.suggest
            else:
                message, suggest = f"TorBox error: {body.get('error')} — {body.get('detail')}", ""
            raise StageError(f"torbox.{stage}", message, suggest, body=body,
                             status=status, retry_after=header(answered, "Retry-After"))
        return body

    def user(self) -> dict:
        return self._call("user", "GET", "/user/me").get("data") or {}

    def check_cached(self, hashes: list[str]) -> dict[str, dict]:
        """One call for up to ~100 hashes. Returns {hash: {name, size, files}} for cached ones only."""
        if not hashes:
            return {}
        out: dict[str, dict] = {}
        for i in range(0, len(hashes), 90):
            chunk = hashes[i:i + 90]
            body = self._call("checkcached", "GET", "/torrents/checkcached",
                              query=f"hash={','.join(chunk)}&format=object&list_files=true")
            data = body.get("data") or {}
            if isinstance(data, dict):
                out.update({k.lower(): v for k, v in data.items()})
        self.cached_hashes.update(out)
        self.log.info("torbox.checkcached", "done", result="PASS", asked=len(hashes), cached=len(out))
        return out

    def _raise_if_refused_not_cached(self, exc: StageError, info_hash: str, only_if_cached: bool, ms: float) -> None:
        """Turn TorBox's cached-only refusal into its own named error; any other failure passes through.

        Documented (Postman collection, `createtorrent` example "Fail DOWNLOAD_NOT_CACHED", read
        2026-09-26): HTTP 400 `{"success": false, "error": "DOWNLOAD_NOT_CACHED", "detail": "Torrent
        not found in cache. Disable the add only if cached parameter to add the download."}`. Keyed
        on the error code, not the status: the Usenet twin of the same refusal is documented as 500.
        A plain `StageError`, never `BlockedError`: an expired cache is about this release, so the
        loop moves to the next candidate and the block guard's run is broken, not extended."""
        body = exc.data.get("body")
        code = str(body.get("error") or "") if isinstance(body, dict) else ""
        if not (only_if_cached and "NOT_CACHED" in code):
            return
        detail = str(body.get("detail") or "").strip()
        suggest = "trying the next candidate; TorBox's cache for this one expired after checkcached"
        self.log.warn("torbox.createtorrent", "not_cached", result="FAIL", error=code, detail=detail,
                      info_hash=info_hash, ms=ms, suggest=suggest,
                      reason="checkcached reported it cached, the cached-only add was refused, nothing was added")
        raise StageError("torbox.createtorrent",
                         f"TorBox no longer has this cached ({code}); the cached-only add was refused",
                         suggest, error_code=code, body=body) from exc

    def _rate_limit_error(self, limit: str, info_hash: str, only_if_cached: bool, sent: bool, ms: float,
                          said: str | None = None, body=None) -> StageError:
        """Log one `torbox.createtorrent rate_limit` line and build its error. One shape for the add
        that got the 429 (`sent=True`) and the add not sent because of it (`sent=False`)."""
        message, suggest = TORBOX_LIMIT_WORDS[limit]
        self.log.warn("torbox.createtorrent", "rate_limit", result="FAIL", info_hash=info_hash, sent=sent,
                      only_if_cached=only_if_cached, limit=limit, said=said, ms=ms, suggest=suggest,
                      reason=("429 on createtorrent; limit attributed by the pool this add draws from "
                              "(no 429 body is documented)") if sent else
                             "this limit already answered 429 in this play; not sending")
        return StageError("torbox.createtorrent", message, suggest, status=429, limit=limit, sent=sent, body=body)

    def _raise_if_rate_limited(self, exc: StageError, info_hash: str, only_if_cached: bool, ms: float) -> None:
        """Name which `createtorrent` limit a 429 came from; any other failure passes through.

        Docs (`createtorrent` "Rate Limits", read 2026-09-26): "300 requests per minute: a shared
        ceiling that every request counts against, whether the torrent is cached or uncached" and
        "60 uncached torrents per hour: an additional, stricter limit that only uncached torrents
        count against." The collection documents no 429 body, so the limit is attributed by which
        pool the request drew from: an add sent without `add_only_if_cached` can only be the hourly
        one in practice (a play sends a handful of adds, not 300 a minute); a cached-only add is
        not capped hourly. UNVERIFIED against a live 429 — the body's own `error`/`detail`, if any,
        is logged beside it. Stays a plain `StageError`: the block guard treats a 429 as transient, so it
        resets the block guard's run instead of extending it.

        The message avoids the word "uncached": `diagnose_failure` reads it as "the download
        carries on there", and here nothing was added."""
        if exc.data.get("status") != 429:
            return
        body = exc.data.get("body")
        said = ""
        if isinstance(body, dict):
            said = " — ".join(str(body.get(k)) for k in ("error", "detail") if body.get(k))
        limit = TORBOX_REQUEST_LIMIT if only_if_cached else TORBOX_UNCACHED_LIMIT
        if not only_if_cached:
            self.uncached_limited = limit
        raise self._rate_limit_error(limit, info_hash, only_if_cached, True, ms, said or None, body) from exc

    def _unavailable_error(self, info_hash: str, only_if_cached: bool, sent: bool, ms: float) -> ProviderUnavailableError:
        """Log one `torbox.createtorrent no_servers` line and build its error, in one shape for the add
        TorBox refused (`sent=True`, warn/FAIL) and each add not sent because of it (`sent=False`,
        info/SKIP: an echo of the one refusal, not a new failure)."""
        seen = self.unavailable or {}
        row = TORBOX_SIDE[TORBOX_NO_SERVERS]
        emit = self.log.warn if sent else self.log.info
        emit("torbox.createtorrent", "no_servers", result="FAIL" if sent else "SKIP", info_hash=info_hash, sent=sent,
             only_if_cached=only_if_cached, error=seen.get("error_code"), detail=seen.get("detail"),
             status=seen.get("status"), retry_after_s=seen.get("retry_after"),
             refused_hash=seen.get("info_hash"), refused_id=seen.get("id"), ms=ms, suggest=row.suggest,
             reason=("TorBox answered NO_SERVERS_AVAILABLE_ERROR: no download server free on its side; "
                     "not counted against this release or the account; no further add TorBox has not "
                     "cached is sent in this play") if sent else
                    "TorBox had no download server free earlier in this play; not sending")
        return ProviderUnavailableError("torbox.createtorrent", row.message, row.suggest, unavailable=row.kind,
                                        error_code=seen.get("error_code"), detail=seen.get("detail"),
                                        status=seen.get("status"), retry_after=seen.get("retry_after"), sent=sent)

    @staticmethod
    def _side_error(exc: StageError, code: str, row: TorBoxSide, info_hash: str, cand_id) -> dict:
        """The one shape of an error on TorBox's side, as `unavailable` and `side_error` hold it and the
        diagnosis carry in `cli._resolve` reads it."""
        body = exc.data.get("body") or {}
        return {"unavailable": row.kind, "error_code": code, "message": row.message, "suggest": row.suggest,
                "detail": str(body.get("detail") or "").strip() or None, "stage": exc.stage,
                "status": exc.data.get("status"), "retry_after": retry_after_s(exc.data.get("retry_after")),
                "info_hash": info_hash, "id": cand_id}

    def _raise_if_torbox_side(self, exc: StageError, info_hash: str, cand_id, ms: float,
                              only_if_cached: bool = False) -> None:
        """A code in `TORBOX_SIDE` becomes a `ProviderUnavailableError`; any other failure passes
        through. Keyed on the error code, not the status (the docs give 400 on one step, 500 on another).
        A row that `stops_adds` (NO_SERVERS) counts on `createtorrent` only, and is remembered for the rest
        of this resolve (`_add` holds back every later add TorBox has not cached); the others hold nothing back."""
        body = exc.data.get("body")
        code = str(body.get("error") or "") if isinstance(body, dict) else ""
        row = TORBOX_SIDE.get(code)
        if row is None or (row.stops_adds and exc.stage != "torbox.createtorrent"):
            return
        found = self._side_error(exc, code, row, info_hash, cand_id)
        if row.stops_adds:
            self.unavailable = found
            self.side_error = found
            raise self._unavailable_error(info_hash, only_if_cached, True, ms) from exc
        if self.side_error is None:
            self.side_error = found
        self.log.warn(exc.stage, "server_side", result="FAIL", info_hash=info_hash, id=cand_id, error=code,
                      unavailable=row.kind, detail=found["detail"], status=found["status"],
                      retry_after_s=found["retry_after"], ms=ms, suggest=row.suggest,
                      reason=f"TorBox answered {code}: an error on its own side; not counted against this release "
                             f"or the account; later adds in this play are still sent")
        raise ProviderUnavailableError(exc.stage, row.message, row.suggest, unavailable=row.kind, error_code=code,
                                       detail=found["detail"], status=found["status"],
                                       retry_after=found["retry_after"], sent=True) from exc

    def listed(self, info_hash: str) -> dict | None:
        """The account's own entry for this hash, if TorBox already holds it.

        Docs (`GET /torrents/mylist`, "Get Torrent List", read 2026-09-27): without `id` it lists the
        account's torrents, each with `id`, `hash`, `progress`, `download_state` and
        `alternative_hashes`; "Default is 1000" per request (`limit`); the list "only gets updated
        every 600 seconds" unless `bypass_cache`; an empty account answers `success: true,
        error: ITEM_NOT_FOUND, data: null`. One page is read: an account past 1000 torrents may miss
        a match, which only means the pack is added again, as before. Read once per client (`account`): a later lookup
        reads the same answer (`fresh` false), and a read that failed fails each later lookup the same way, unread."""
        t0 = time.monotonic()
        info_hash = str(info_hash).lower()
        fresh = self.account is None and self.account_error is None
        if self.account_error is not None:
            self.log.info("torbox.mylist", "listed", result="SKIP", info_hash=info_hash, fresh=False,
                          error=str(self.account_error),
                          reason="the account's list could not be read earlier in this play; not read again")
            raise self.account_error
        if fresh:
            try:
                # The whole account, up to 1000 torrents: read to the larger cap.
                data = self._call("mylist", "GET", "/torrents/mylist", query="bypass_cache=true",
                                  max_bytes=LARGE_MAX_BYTES).get("data")
            except StageError as exc:
                self.account_error = exc
                raise
            self.account = data if isinstance(data, list) else []
        entries = self.account
        entry = next((e for e in entries if isinstance(e, dict) and (
            str(e.get("hash") or "").lower() == info_hash
            or info_hash in [str(a).lower() for a in e.get("alternative_hashes") or []])), None)
        self.log.info("torbox.mylist", "listed", result="PASS" if entry else "EMPTY", info_hash=info_hash,
                      listed=len(entries), fresh=fresh, torrent_id=(entry or {}).get("id"),
                      progress=(entry or {}).get("progress"), download_state=(entry or {}).get("download_state"),
                      ms=(time.monotonic() - t0) * 1000,
                      reason="already on the account; not added again" if entry else
                             "not on the account (whether an add is sent is the add's own line)")
        return entry

    def retry_failed_listing(self, why: str) -> bool:
        """Forget a remembered failure of the account's one read (`account_error`), so the next lookup reads it again:
        at the search past a pinned attempt whose read failed, the search's own lookups are not answered by the pin's
        failure. A list that was read stays as it is. True when a failure was forgotten."""
        if self.account_error is None:
            return False
        self.log.info("torbox.mylist", "retry", result="PASS", error=str(self.account_error), reason=why)
        self.account_error = None
        return True

    def _listed_or_none(self, info_hash: str, cand_id, any_error_adds: bool = False) -> dict | None:
        """`listed`, but a lookup that failed is only a lookup: the release is added as before (`_add`, whose own
        errors are read as before). A refused account and a stop still end the resolve. An error code of TorBox's own
        side (`TORBOX_SIDE`) is read as such (no add during it), as a pinned pack's lookup always read it, unless
        `any_error_adds` (the wait's path: the one add in place of a held dub, "Keep waiting"), where it falls through
        to the add too."""
        t0 = time.monotonic()
        try:
            return self.listed(info_hash)
        except (BlockedError, Interrupted):
            raise
        except StageError as exc:
            if not any_error_adds:
                self._raise_if_torbox_side(exc, info_hash, cand_id, (time.monotonic() - t0) * 1000)
            self.log.warn("torbox.mylist", "listed", result="FAIL", info_hash=info_hash, id=cand_id, error=str(exc),
                          status=exc.data.get("status"),
                          suggest="the account's list could not be read; the release is added as before")
            return None

    def resolve(self, cand: dict, pick_file: str | None = None, reuse_listed: bool = False,
                lookup_only: bool = False) -> dict:
        """createtorrent → mylist → requestdl. Returns the same shape as RealDebrid.resolve.

        `reuse_listed`: for a pinned pack TorBox has not cached, first look it up in
        the account's list and, when it is there, use that entry instead of adding it again. `lookup_only`: the lookup
        is only a lookup, whatever it answered (the wait's path): a failed one, TorBox's own side included, adds."""
        t0 = time.monotonic()
        info_hash = str(cand["info_hash"]).lower()
        # A release this play's `checkcached` reported cached is added cached-only, so a cache
        # that expired since then is refused instead of starting a download the play will not wait
        # for (every uncached add counts against TorBox's 60-per-hour budget). An uncached release
        # is added as before: that add *is* the fallback where the download carries on at TorBox.
        only_if_cached = info_hash in self.cached_hashes
        try:
            entry = (self._listed_or_none(info_hash, cand.get("id"), any_error_adds=lookup_only)
                     if reuse_listed and not only_if_cached else None)
            if entry is not None and entry.get("id") is not None:
                torrent_id, was_cached, reused = entry["id"], False, True
            else:
                (torrent_id, was_cached), reused = self._add(cand, info_hash, only_if_cached, t0), False
            if not (only_if_cached or was_cached):
                self.downloads.append({"torrent_id": torrent_id, "info_hash": info_hash, "id": cand.get("id"),
                                       "reused": reused})
                if not reused and isinstance(self.account, list):
                    # The one listing was read before this add: it joins the list, so a later lookup in this play (the
                    # search past a pin that just added its pack) finds it instead of adding it again.
                    self.account.append({"id": torrent_id, "hash": info_hash})
            return self._stream(cand, torrent_id, reused, was_cached, pick_file, t0)
        except (BlockedError, ProviderUnavailableError):
            raise
        except StageError as exc:
            self._raise_if_torbox_side(exc, info_hash, cand.get("id"), (time.monotonic() - t0) * 1000)
            raise

    def _add(self, cand: dict, info_hash: str, only_if_cached: bool, t0: float) -> tuple:
        """`createtorrent`, with the cached-only and hourly-limit rules (`cached_hashes`, `uncached_limited`): (the new
        torrent's id, whether TorBox had it cached)."""
        form = {"magnet": f"magnet:?xt=urn:btih:{cand['info_hash']}", "seed": TORBOX_NO_SEED, "allow_zip": "false"}
        if only_if_cached:
            form["add_only_if_cached"] = "true"
        elif self.unavailable is not None:
            raise self._unavailable_error(info_hash, only_if_cached, False, (time.monotonic() - t0) * 1000)
        elif self.uncached_limited is not None:
            raise self._rate_limit_error(self.uncached_limited, info_hash, only_if_cached, False,
                                         (time.monotonic() - t0) * 1000)
        try:
            body = self._call("createtorrent", "POST", "/torrents/createtorrent", form=form)
        except StageError as exc:
            ms = (time.monotonic() - t0) * 1000
            self._raise_if_refused_not_cached(exc, info_hash, only_if_cached, ms)
            self._raise_if_torbox_side(exc, info_hash, cand.get("id"), ms, only_if_cached)
            self._raise_if_rate_limited(exc, info_hash, only_if_cached, ms)
            raise
        data = body.get("data") or {}
        torrent_id = data.get("torrent_id") or data.get("queued_id")
        cached_now = "cached" in str(body.get("detail", "")).lower()
        self.log.info("torbox.createtorrent", "done", result="PASS", torrent_id=torrent_id, detail=body.get("detail"),
                      seed=TORBOX_NO_SEED, only_if_cached=only_if_cached, ms=(time.monotonic() - t0) * 1000)
        if not torrent_id:
            raise StageError("torbox.createtorrent", f"no torrent_id in response: {body}", body=body)
        return torrent_id, cached_now

    def wait_ready(self, torrent_id, budget_s: float, on_poll=None, progress: int = 0, state: str | None = None,
                   holds: bool = False, on_tick=None, downloaded=None) -> dict:
        """Wait for TorBox to download a torrent this play added: `{ready, torrent_id, status, progress, eta,
        downloaded, size, speed, left_s, polls, waited_s, ended}`, `ready` False when `budget_s` runs out on the wall
        clock (`ended` "budget"; suspended time counts). Nothing else ends it here: not TorBox's estimate, however far
        past anything (the viewer decides, with the numbers). `progress`, `state` and `downloaded` are what the add
        said, until a poll says more. Adds nothing and asks for no link (`link` does, so its errors are read as the
        resolve's own are). `holds`: the caller holds the viewer's choices when the budget runs out, so its end is
        logged as a wait (`WAIT`), not a failure `trace` would headline.

        Polls `mylist` every `WAIT_POLL_S` from one poll's start to the next (`poll_gap`; a failed poll backs off),
        never past the budget. Every poll logs `torbox.wait poll` with the time since the wait began, TorBox's state,
        progress, bytes, speed and estimate, the rate worked out from the bytes (`ByteRate`) with its reason, and the
        next gap with its reason. `on_poll(reading)` (`{state, progress, eta, downloaded, size, finished, present,
        speed, left_s}`, the last poll's; `speed` and `left_s` are the rate's, never TorBox's `eta`) runs before and
        after each one (the play's status line), a failed one included, and every `WAIT_CAP_S` of a longer gap;
        `on_tick()` runs every `WAIT_SLICE_S` of the sleep (the caller's own state, e.g. the viewer's "Keep waiting").
        Each poll is one attempt, its socket timeout no longer than the time left (`WAIT_POLL_TIMEOUT_S` at most; it
        bounds each socket operation, not the whole request). A failed poll is logged and waited past; a refused
        account (`BlockedError`) ends the wait, and so does a stop (`Interrupted`, raised by the caller's signal
        handler), also when it lands during a poll's request."""
        clock = _wall_clock
        t0 = clock()
        polls = failures = 0
        rate = ByteRate()
        beat = on_poll or (lambda reading: None)
        tick = on_tick or (lambda: None)
        last = {"provider": "torbox", "ready": False, "cached": False, "torrent_id": torrent_id, "status": state,
                "progress": progress or 0, "eta": None, "downloaded": downloaded, "size": None, "finished": None,
                "present": None, "speed": None, "left_s": None, "reused": False, "polls": 0, "waited_s": 0.0,
                "ended": "budget"}

        def count_age() -> tuple[float | None, bool]:
            """The newest byte count's age, and whether a line built on it is still fresh by the rate's own rule (first
            seen at most `RATE_STALE_S` ago, the age at which an answered poll drops the speed and time left too)."""
            age = rate.newest_age(clock())
            return age, age is not None and age <= RATE_STALE_S

        def reading() -> dict:
            # `downloaded` (TorBox's `total_downloaded`): a `checking` with nothing downloaded is worded as the start
            # it is (`say._reading_kind`), not as a check of what it has. `finished`/`present`: a download TorBox has
            # finished is worded as finishing or had, whatever its counters say.
            if failures and (last["speed"] is not None or last["left_s"] is not None) and not count_age()[1]:
                # The one place a line kept through failed polls goes: once its newest count is stale, at the failed
                # poll's own write or in the middle of its backoff (which can last a minute).
                last.update(speed=None, left_s=None)
            return {k: last[k] for k in ("progress", "eta", "downloaded", "size", "finished", "present", "speed",
                                         "left_s")} | {"state": last["status"]}

        def nap(seconds: float) -> None:
            """Sleep `seconds` in slices: `on_tick` after each, the status rewritten every `WAIT_CAP_S`."""
            slept = since_beat = 0.0
            while slept < seconds:
                piece = min(WAIT_SLICE_S, seconds - slept)
                _wait_sleep(piece)
                slept += piece
                since_beat += piece
                tick()
                if since_beat >= WAIT_CAP_S and slept < seconds:
                    beat(reading())
                    since_beat = 0.0

        gap, why = poll_gap()
        started = t0                             # the last poll's start: the next one is `gap` after it
        while True:
            left = budget_s - (clock() - t0)
            if left <= 0:
                break
            took = max(0.0, min(left, started + gap - clock()))
            nap(took)
            started = clock()
            polls += 1
            since = round(started - t0, 1)
            last.update(polls=polls, waited_s=since)
            beat(reading())
            timeout = max(1.0, min(WAIT_POLL_TIMEOUT_S, budget_s - (clock() - t0)))
            try:
                lst = self._call("mylist", "GET", "/torrents/mylist", query=f"id={torrent_id}&bypass_cache=true",
                                 retries=1, timeout=timeout).get("data")
            except (BlockedError, Interrupted):
                raise
            except StageError as exc:
                failures += 1
                limited = exc.data.get("status") == 429
                gap, why = poll_gap(failures, exc.data.get("retry_after"), _wait_random())
                # No answer, so no new count. The line keeps its speed and time left while they are still fresh by the
                # rate's own rule (`count_age`); `reading()`, at this poll's write below, drops them once they are not.
                # The rate's counts are kept.
                age, fresh = count_age()
                had = last["speed"] is not None or last["left_s"] is not None
                held = had and fresh
                self.log.warn("torbox.wait", "poll", result="FAIL", torrent_id=torrent_id, poll=polls, since_s=since,
                              error=str(exc), status=exc.data.get("status"), rate_limited=limited,
                              retry_after=exc.data.get("retry_after"), failures=failures,
                              timeout_s=round(timeout, 1), nap_s=round(took, 2), next_s=round(gap, 1), next_why=why,
                              line_kept=held, count_age_s=None if age is None else round(age, 1),
                              kept_why=(f"the newest byte count is {age:.0f} s old, within {RATE_STALE_S:g} s: the "
                                        "line keeps its speed and time left") if held else
                                       "no speed or time left to keep" if not had or age is None else
                                       f"the newest byte count is {age:.0f} s old, past {RATE_STALE_S:g} s: the speed "
                                       "and time left go, the percentage stays",
                              suggest="TorBox's rate limit answered (429): the wait backs off and goes on" if limited
                              else "one poll failed; the wait backs off a little and goes on, and the next poll is "
                                   "the retry")
                # A failure's backoff counts from the poll's end, so a poll that hung for its whole timeout still
                # waits its backoff (from its start, an 8 s timeout would swallow a 6 s backoff whole).
                started = clock()
                beat(reading())
                continue
            failures = 0
            entry = lst[0] if isinstance(lst, list) and lst else (lst if isinstance(lst, dict) else {})
            ready = bool(entry.get("download_finished") and entry.get("download_present"))
            progress = _percent(entry.get("progress"))
            seen = rate.observe(started, entry.get("download_state"), entry.get("total_downloaded"),
                                entry.get("size"), entry.get("download_speed"), entry.get("seeds"))
            last.update(status=entry.get("download_state"), progress=progress, eta=entry.get("eta"),
                        downloaded=entry.get("total_downloaded"), size=entry.get("size"),
                        finished=entry.get("download_finished"), present=entry.get("download_present"),
                        speed=seen["speed"], left_s=seen["left_s"])
            gap, why = poll_gap()
            if seen["refused"]:
                self.log.warn("torbox.wait", "bytes", result="FAIL", torrent_id=torrent_id, poll=polls,
                              since_s=since, downloaded=entry.get("total_downloaded"), size=entry.get("size"),
                              reason=seen["refused"],
                              suggest="TorBox's byte count is not used for the speed or the time left; the next "
                                      "count that fits is")
            if seen["restarted"]:
                self.log.info("torbox.wait", "bytes", result="RESET", torrent_id=torrent_id, poll=polls,
                              since_s=since, downloaded=entry.get("total_downloaded"), size=entry.get("size"),
                              reason=seen["restarted"])
            self.log.info("torbox.wait", "poll", result="PASS" if ready else "WAIT", torrent_id=torrent_id, poll=polls,
                          since_s=since, state=entry.get("download_state"), progress=progress,
                          size=entry.get("size"), downloaded=entry.get("total_downloaded"),
                          speed=entry.get("download_speed"), eta=entry.get("eta"), seeds=entry.get("seeds"),
                          peers=entry.get("peers"), finished=entry.get("download_finished"),
                          present=entry.get("download_present"), rate_bps=seen["rate"], rate_why=seen["why"],
                          since_change_s=seen["since_change_s"], speed_shown=seen["speed"], left_s=seen["left_s"],
                          budget_s=budget_s, nap_s=round(took, 2), next_s=None if ready else round(gap, 1),
                          next_why=None if ready else why)
            beat(reading())
            if ready:
                last.update(ready=True, waited_s=round(clock() - t0, 1), ended="ready")
                self.log.info("torbox.wait", "ready", result="PASS", torrent_id=torrent_id, polls=polls,
                              waited_s=last["waited_s"])
                return last
        last["waited_s"] = round(clock() - t0, 1)
        if holds:
            self.log.info("torbox.wait", "timeout", result="WAIT", torrent_id=torrent_id, polls=polls,
                          waited_s=last["waited_s"], state=last["status"], progress=last["progress"], eta=last["eta"],
                          left_s=last["left_s"], budget_s=budget_s,
                          reason="held: the wait reached its cap and the play keeps the viewer's choices; the download "
                                 "carries on in TorBox")
        else:
            self.log.warn("torbox.wait", "timeout", result="FAIL", torrent_id=torrent_id, polls=polls,
                          waited_s=last["waited_s"], state=last["status"], progress=last["progress"], eta=last["eta"],
                          left_s=last["left_s"], budget_s=budget_s, suggest="the download carries on in TorBox")
        return last

    def link(self, cand: dict, torrent_id, pick_file: str | None = None) -> dict:
        """The link to a torrent the play waited for (`_stream`), its errors read as `resolve` reads them: a code of
        TorBox's own side is a `ProviderUnavailableError`, a refused account a `BlockedError`, anything else the
        plain `StageError` the loop records for that release."""
        t0 = time.monotonic()
        try:
            return self._stream(cand, torrent_id, False, False, pick_file, t0)
        except (BlockedError, ProviderUnavailableError):
            raise
        except StageError as exc:
            self._raise_if_torbox_side(exc, str(cand.get("info_hash") or "").lower(), cand.get("id"),
                                       (time.monotonic() - t0) * 1000)
            raise

    def _stream(self, cand: dict, torrent_id, reused: bool, was_cached: bool, pick_file: str | None,
                t0: float) -> dict:
        """mylist → requestdl for a torrent on the account."""
        entry = None
        for _ in range(3):
            lst = self._call("mylist", "GET", "/torrents/mylist", query=f"id={torrent_id}&bypass_cache=true").get("data")
            entry = lst[0] if isinstance(lst, list) and lst else (lst if isinstance(lst, dict) else None)
            if entry:
                break
            time.sleep(1.0)
        if not entry:
            raise StageError("torbox.mylist", f"torrent {torrent_id} not listed", torrent_id=torrent_id)
        state = entry.get("download_state")
        files = [f for f in entry.get("files") or [] if isinstance(f, dict)]
        # Cut as the torrent info's and `checkcached`'s names are, so the picked file is found here by its path.
        core.log_names_cut(self.log, "torbox.mylist", [str(f.get("name") or "") for f in files], torrent_id=torrent_id)
        core.log_sizes_refused(self.log, "torbox.mylist", [f.get("size") for f in files], torrent_id=torrent_id)
        files = [{**f, **{k: core.cut_file_name(str(f[k])) for k in ("name", "short_name") if f.get(k)},
                  **({"size": core.file_size(f["size"])} if core.size_refused(f.get("size")) else {})} for f in files]
        self.log.info("torbox.mylist", "state", torrent_id=torrent_id, download_state=state, finished=entry.get("download_finished"),
                      present=entry.get("download_present"), progress=entry.get("progress"), files=len(files),
                      seed_torrent=entry.get("seed_torrent"), long_term_seeding=entry.get("long_term_seeding"),
                      size=entry.get("size"), downloaded=entry.get("total_downloaded"))
        if not (entry.get("download_finished") and entry.get("download_present")):
            return {"provider": "torbox", "cached": False, "torrent_id": torrent_id, "status": state,
                    "progress": _percent(entry.get("progress")), "reused": reused,
                    "downloaded": entry.get("total_downloaded")}
        video = [f for f in files if str(f.get("mimetype", "")).startswith("video") or str(f.get("name", "")).lower().endswith((".mkv", ".mp4"))]
        chosen = None
        if pick_file:
            chosen = next((f for f in video if str(f.get("name", "")).endswith(pick_file)), None)
        if chosen is None and cand.get("file"):
            chosen = handed_over(self.log, "torbox.filepick", video, cand["file"], torrent_id=torrent_id)
        if chosen is None and len(video) > 1 and cand.get("episode"):
            skipped: list = []
            picked = pick_episode_file(video, cand["episode"], cand.get("alt"),
                                       identity_for(self.log, "torbox.filepick", cand), cand.get("title") or "", skipped)
            log_first_season_pick(self.log, "torbox.filepick", cand, picked, skipped)
            if picked:
                chosen = picked
                self.log.info("torbox.filepick", "pick", result="PASS", torrent_id=torrent_id, file=picked["basename"],
                              matched=picked["matched"], identity=picked.get("identity"), read=picked.get("read"),
                              **skipped_summary(skipped, cand.get("entry")))
            elif (cand.get("parsed") or {}).get("batch"):
                aside = set_aside_words(skipped)
                raise StageError("torbox.filepick", f"no file for episode {cand['episode']} in torrent {torrent_id}"
                                 + (f"; set aside: {aside}" if aside else ""),
                                 pick_refused_suggest(skipped), torrent_id=torrent_id,
                                 files=[f.get("short_name") or f.get("name") for f in video][:20],
                                 **skipped_summary(skipped, cand.get("entry")))
            else:
                # A single's torrent with no file read as the episode plays its largest
                # video, as before; say so, with what was passed over.
                self.log.warn("torbox.filepick", "fallback", result="SKIP", torrent_id=torrent_id, files=len(video),
                              **skipped_summary(skipped, cand.get("entry")),
                              suggest="no file read as this episode; the largest video plays (not a pack)")
        chosen = chosen or (max(video, key=lambda f: f.get("size") or 0) if video else (files[0] if files else None))
        if chosen is None:
            raise StageError("torbox.mylist", "torrent has no files", torrent_id=torrent_id)
        dl = self._call("requestdl", "GET", "/torrents/requestdl",
                        query=f"token={self.token}&torrent_id={torrent_id}&file_id={chosen['id']}")
        url = dl.get("data")
        if not isinstance(url, str) or not url:
            raise StageError("torbox.requestdl", f"no URL in response: {type(url).__name__}", body=dl)
        check_stream(self.log, "torbox.stream", url, torrent_id=torrent_id, file_id=chosen["id"])
        self.log.info("torbox.requestdl", "done", result="PASS", host=link_host(url), file_id=chosen["id"],
                      ms=(time.monotonic() - t0) * 1000)
        return {"provider": "torbox", "cached": True, "download": url, "filename": chosen.get("short_name") or chosen.get("name"),
                "filesize": chosen.get("size"), "torrent_id": torrent_id, "status": state, "progress": 100,
                "was_cached": was_cached, "reused": reused}


def link_host(url) -> str | None:
    """A link's host for a log line: never its user part, path or query, and None for one that does not parse."""
    try:
        return core.url_origin(str(url))[1] or None
    except ValueError:
        return None


def stream_refusal(url) -> dict | None:
    """Why a provider's stream link may not be played, or None: `core.public_https_refusal`, the rule covers and the
    redirect guard use too. mpv and urllib open `file:`, `data:`, `edl://`, `av://` and plain `http://` (a TorBox link
    carries the account key as `?token=`), and a loopback or LAN host is the machine's own network. The answer names
    the scheme and host only, never the query."""
    reason = core.public_https_refusal(url)
    if reason is None:
        return None
    try:
        scheme = urllib.parse.urlsplit(str(url or "")).scheme or None
    except ValueError:
        scheme = None
    return {"reason": reason, "scheme": scheme, "host": link_host(url)}


def check_stream(log: Log, component: str, url, **data) -> None:
    """Raise a `StageError` for a link `stream_refusal` refuses: the release fails as one that cannot be played, and the
    next one is tried. One FAIL line with the scheme and host, never the link."""
    refused = stream_refusal(url)
    if refused is None:
        return
    log.warn(component, "refused", result="FAIL", **refused, **data,
             suggest="the provider's stream link is not https to a public host; this release is skipped")
    raise StageError(component, f"the stream link is not https to a public host ({refused['reason']})",
                     "the next release is tried", **refused, **data)


# What the probe's concat script may open. The script itself comes in over `pipe`; the stream over http(s).
# FFmpeg's only proxy is an `http://` value of `http_proxy` (libavformat http.c and tls.c): an http URL reaches it over
# `tcp`, an https one tunnels through `httpproxy`. It never reads `https_proxy`, `all_proxy` or a `socks5://` value,
# so no proxy setup probes differently from the argv form. Not `file`: the script names nothing on this machine.
PROBE_PROTOCOLS = "pipe,http,https,tcp,tls,httpproxy"


def _concat_token(text: str) -> str:
    """`text` as one ffconcat token: all of it inside single quotes, each `'` closed, escaped and reopened (man
    ffmpeg-utils, "Quoting and escaping": the quote character itself cannot be quoted)."""
    return "'" + text.replace("'", "'\\''") + "'"


def probe_tracks(log: Log, url: str, timeout: int = 25, audio_sink: list | None = None) -> list[str] | None:
    """Read subtitle track languages from the container header over HTTP with ffprobe (exact ISO 639-2 tags).
    Returns None when ffprobe is missing or fails. `audio_sink`, when given, receives the audio tracks' languages from
    the same read (`und` for an untagged one), which the dub reading takes; nothing is read twice.

    The URL is never an argument. A TorBox stream URL carries the account's API key as `?token=`, and a
    child's argv is readable by every local user (`/proc/<pid>/cmdline`). ffprobe reads it from stdin instead, as a
    one-file concat script (man ffmpeg-formats, "concat"): `-safe 0` lets a `file` be a URL, `option` passes the user
    agent to that file's open, and the file is opened under `-protocol_whitelist`.

    Measured 2026-09-15, live, in the argv form: 0.9 s on a TorBox CDN link. Measured 2026-10-18, offline, against a
    loopback Range server at 20 MB/s: the stdin form reads the same languages on MKV and MP4 (its index first or last)
    with the same requests; about 9 % slower than the argv form on a 77 MB MKV (1.58 s against 1.46 s), within noise
    on the others."""
    t0 = time.monotonic()
    if any(ord(c) < 32 or ord(c) == 127 for c in url):
        # One line per directive: a line break in the URL would be a directive of its own.
        log.warn("verify.ffprobe", "refused", result="FAIL", reason="control character in the stream URL",
                 suggest="the release is not probed; its languages stay unknown")
        return None
    script = f"ffconcat version 1.0\nfile {_concat_token(url)}\noption user_agent {_concat_token(USER_AGENT)}\n"
    cmd = ["ffprobe", "-v", "error", "-f", "concat", "-safe", "0", "-protocol_whitelist", PROBE_PROTOCOLS,
           "-show_entries", "stream=index,codec_type:stream_tags=language,title", "-of", "json", "-i", "pipe:0"]
    try:
        proc = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        log.warn("verify.ffprobe", "missing", result="FAIL", suggest="install ffmpeg (provides ffprobe)")
        return None
    except subprocess.TimeoutExpired:
        log.warn("verify.ffprobe", "timeout", result="FAIL", ms=(time.monotonic() - t0) * 1000, timeout=timeout)
        return None
    if proc.returncode != 0:
        # The concat form prints the URL before the reason (`Impossible to open '<url>'` / `pipe:0: <reason>`), so a
        # TorBox-length URL can push the reason past `err`'s cut: the last line is kept whole as `cause`.
        lines = proc.stderr.strip().splitlines()
        log.warn("verify.ffprobe", "probe", result="FAIL", ms=(time.monotonic() - t0) * 1000, rc=proc.returncode,
                 cause=(lines[-1] if lines else "")[:200], err=proc.stderr.strip()[:200],
                 suggest="the release plays unprobed; `cause` and `err` are ffprobe's own words (the concat `option` "
                         "directive needs FFmpeg 5.0 or later)")
        return None
    try:
        streams = json.loads(proc.stdout or "{}").get("streams") or []
    except json.JSONDecodeError as exc:
        log.warn("verify.ffprobe", "parse", result="FAIL", ms=(time.monotonic() - t0) * 1000, error=str(exc),
                 out=(proc.stdout or "")[:200],
                 suggest="the release plays unprobed; ffprobe exited 0 without JSON (`out`): check `ffprobe -version`")
        return None
    langs = [str((st.get("tags") or {}).get("language") or "und") for st in streams if st.get("codec_type") == "subtitle"]
    audio = [str((st.get("tags") or {}).get("language") or "und") for st in streams if st.get("codec_type") == "audio"]
    log.info("verify.ffprobe", "probe", result="PASS", ms=(time.monotonic() - t0) * 1000, subs=sorted(set(langs)),
             audio=audio)
    if audio_sink is not None:
        audio_sink[:] = audio
    return langs


def range_check(http: Http, log: Log, url: str) -> dict:
    """Does the RD download host honour HTTP Range? Expect 206 + Content-Range."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-1"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=http.timeout) as resp:
            status, final = resp.status, resp.url
            cr = resp.headers.get("Content-Range")
            ar = resp.headers.get("Accept-Ranges")
            resp.read(2)
    except urllib.error.HTTPError as exc:
        status, final = exc.code, exc.url
        cr, ar = exc.headers.get("Content-Range"), exc.headers.get("Accept-Ranges")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # A debrid host can be down or accept the connection and send nothing. Either way the stream
        # is unusable right now.
        err = f"{type(exc).__name__}: {exc}" if not isinstance(exc, TimeoutError) else f"timed out after {http.timeout}s"
        log.warn("rd.rangeCheck", "probe", result="FAIL", ms=(time.monotonic() - t0) * 1000, host=urllib.parse.urlparse(url).netloc,
                 error=err, suggest="stream has no data yet or the host is down; the next candidate is tried")
        return {"seekable": False, "reachable": False, "status": None, "content_range": None, "accept_ranges": None,
                "error": err}
    ok = status == 206 and bool(cr)
    # A redirect is followed (the link's own token goes with it, as it does when mpv opens the link): the line names
    # the origin it ended at, never its path or query.
    moved = {"moved_to": core.origin_label(final)} if final and core.url_origin(final) != core.url_origin(url) else {}
    log.info("rd.rangeCheck", "probe", result="PASS" if ok else "FAIL", ms=(time.monotonic() - t0) * 1000, status=status,
             content_range=cr, accept_ranges=ar, host=urllib.parse.urlparse(url).netloc, **moved,
             suggest="" if ok else "seeking may not work: the host did not answer a range request with 206")
    return {"seekable": ok, "reachable": True, "status": status, "content_range": cr, "accept_ranges": ar}


