"""Prowlarr detection and expansion for the built-in Torznab add-on.

One entry point, `resolve`, used by a play's worker pool and by the Accounts check alike: what a server is
(remembered, or detected now: Prowlarr's status endpoint, then `t=caps`; words that name why when neither), then
either its one Torznab feed searched, or a Prowlarr's enabled torrent indexers with an anime or movie category
returned as feeds (`{pasted base}/{id}/api`, the key in `X-Api-Key` only). `known_feeds` is the no-network read
of the same memory, for the registry's count.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from . import core
from . import torznab as tz


DETECT_TTL_S = 30 * 86400             # what a server is, once found (a check, a new key or a new address asks again)
LIST_TTL_S = 24 * 3600                # a Prowlarr's indexer list, until the Accounts check reads it again
LIST_STALE_S = 7 * 86400              # an older list still serves while Prowlarr cannot be reached to refresh it
STATUS_MAX_BODY = 256 * 1024          # the status body is ~1 KB
LIST_MAX_BODY = 16 * 2**20            # every indexer with its settings and category tree; ~10 KB each
MAX_INDEXERS = 50                     # indexer feeds of one Prowlarr searched; the rest are counted
MAX_CATEGORY_NODES = 20000            # nodes walked in one indexer's category tree
LIST_TRANSIENT = ("unreachable", "timeout", "rate_limited")      # and a 5xx: a stale list serves meanwhile
_NOT_AN_ADDRESS = ("not_torznab", "not_xml")


def _records_dir():
    return tz.cache_root() / "servers"


def _record_path(ctx, src: tz.Source):
    return tz.cache_path(ctx, "servers", src.record_key)


CARRIED = ("indexers", "list_ts", "dropped", "total")    # a Prowlarr's list, kept across a new verdict


def _carry(prior: dict | None) -> dict:
    """The remembered indexer list of a record, to keep in whatever record replaces it: a check,
    a re-detection or a refusal never erases the list a later play may serve stale."""
    return {k: prior[k] for k in CARRIED if prior and k in prior}


def _record(path, src: tz.Source, max_age: float | None = None) -> dict | None:
    """The server's remembered detection, when it was made with this key: `{kind, key, ts, ...}` where `kind`
    is `prowlarr` (with `indexers`, `list_ts`, `dropped`), `torznab`, or `none` (a definite refusal: `miss`,
    `message`). None when there is none, it is older than `DETECT_TTL_S`, or the key changed."""
    data = core.cached_json(path, None) if path is not None else None
    if not isinstance(data, dict) or data.get("key") != tz._fingerprint(src.key):
        return None
    if max_age is not None and time.time() - float(data.get("ts") or 0) > max_age:
        return None
    return data


def _indexer_feeds(src: tz.Source, kept: list) -> list[tz.Feed]:
    """`{pasted base}/{id}/api` for each kept indexer, the key in the `X-Api-Key` header."""
    out = []
    for i in kept:
        if not (isinstance(i, dict) and type(i.get("id")) is int and i["id"] > 0):
            continue
        url = f"{src.base}/{i['id']}/api"
        out.append(tz.Feed(url=url, key=src.key, label=tz.feed_label(url), name=src.server.name, header=True,
                        server=src.label, indexer=str(i.get("name") or f"indexer {i['id']}"), indexer_id=i["id"]))
    return out


def _server_feed(src: tz.Source) -> tz.Feed:
    """The server itself as a `FeedResult`'s feed, for a failure before any indexer feed exists."""
    return tz.Feed(url=src.base, key=src.key, label=src.label, name=src.server.name, header=True, server=src.label)


def _get_json(job: tz.Job, src: tz.Source, path: str, cap: int, retries: int,
              expected: tuple[int, ...] = ()) -> tuple[int, object, dict]:
    """One GET of Prowlarr's JSON API through `tz._send`: `X-Api-Key` only, never `?apikey=`.
    Returns (status, parsed JSON or None when the body is not JSON, headers); the body is parsed here,
    never logged (an indexer's `fields` carry its secrets), not even the first bytes of an error body (`tz._send`
    never logs one: a Prowlarr error body carries a stack trace with its local paths)."""
    url = src.base + path
    headers = {"Accept": "application/json", **({"X-Api-Key": src.key} if src.key else {})}
    status, body, answered = tz._send(job, url, headers, log_url=url, cap=cap, quick=True, retries=retries,
                                      mask=src.feed.mask, expected=expected)
    parsed = None
    if isinstance(body, (bytes, bytearray)) and body:
        try:
            parsed = json.loads(bytes(body))
        except (ValueError, RecursionError):
            parsed = None
    elif isinstance(body, (dict, list)):
        parsed = body
    return status, parsed, answered or {}


@dataclass
class Probe:
    """What detection step 1 (`GET {base}/api/v1/system/status`) found: `prowlarr` when a 200 JSON body says
    `appName: "Prowlarr"`, else how it failed (for step 3's words). `inconclusive` when the answer says nothing
    about what the server is (no answer, a 5xx, a 429, an oversized or redirected answer); only a
    200, a 401 or a 404 is a verdict. `failure` is then the `FeedError` it amounts to."""
    prowlarr: bool = False
    status: int | None = None
    www_auth: bool = False
    unreachable: bool = False
    inconclusive: bool = False
    failure: object = None
    app: str = ""
    version: str = ""
    url_base: str = ""


def _probe_status(job: tz.Job, src: tz.Source) -> Probe:
    """Detection step 1. Judged by the body's shape, never the status alone: a non-API
    Prowlarr path answers the web UI, and every Prowlarr API path answers a wrong key with an empty 401. Only
    `appName`, `version` and `urlBase` are read or logged (the body also carries local paths)."""
    t0 = time.monotonic()
    try:
        # A 404 is the usual answer of anything that is not a Prowlarr (Jackett, a plain feed): an expected
        # answer, so `Http` logs it at debug and every FAIL line in a trace is still a real failure.
        status, data, headers = _get_json(job, src, "/api/v1/system/status", STATUS_MAX_BODY, retries=1,
                                          expected=(404,))
    except tz.FeedError as exc:
        job.say("info", "detect", result="MISS", ms=(time.monotonic() - t0) * 1000, server=src.label,
                step="status", kind=exc.kind, error=str(exc))
        return Probe(unreachable=exc.kind in ("unreachable", "timeout"), inconclusive=True, failure=exc)
    probe = Probe(status=status, www_auth=bool(tz._header(headers, "WWW-Authenticate")))
    if status not in (200, 401, 404):
        probe.inconclusive = True
        probe.failure = tz.status_error(status, headers, " on Prowlarr's status endpoint")
    if status == 200 and isinstance(data, dict):
        probe.app = str(data.get("appName") or "")[:40]
        probe.prowlarr = probe.app == "Prowlarr"
        probe.version, probe.url_base = str(data.get("version") or "")[:40], str(data.get("urlBase") or "")[:200]
    job.say("info" if probe.prowlarr else "debug", "detect", result="PASS" if probe.prowlarr else "MISS",
            ms=(time.monotonic() - t0) * 1000, server=src.label, step="status", status=status,
            json=isinstance(data, dict), app=probe.app or None,
            **({"version": probe.version, "url_base": probe.url_base} if probe.prowlarr else {}),
            reason=None if probe.prowlarr else "not Prowlarr's status answer; trying t=caps (step 2)")
    return probe


def _detected(exc: tz.FeedError, probe: Probe, src: tz.Source) -> tz.FeedError:
    """Detection step 3: the caps probe failed; name why, in this order.
    1. A login (a 401 with `WWW-Authenticate` on either probe): something in front of the server.
    2. A wrong key (an empty 401 on either probe, or `<error code="100">`).
    3. Any other answer that names itself (another `<error>` with its description, a rate limit, a redirect to
       another host, an unsafe or oversized answer, a server error): as it is.
    4. The status probe was inconclusive (no answer, a 5xx, a 429, ...) and caps found no Torznab feed: a Prowlarr
       whose status call failed answers the caps probe with its `/api` JSON, so this is the status failure, not
       "not an address". Never remembered.
    5. Anything else: "not a Torznab or Prowlarr address"; for a status 404 and a JSON caps answer (the viewer
       pasted Prowlarr's own `…/api`), in words that say so."""
    kind = exc.kind
    if kind == "login" or (probe.status == 401 and probe.www_auth):
        return tz.FeedError("login", "the server asks for a login (HTTP 401 with WWW-Authenticate): something in "
                                  "front of it wants a user name and password")
    not_address = kind in _NOT_AN_ADDRESS or (kind == "http_status" and 400 <= (exc.status or 0) < 500)
    if kind == "wrong_key" or (probe.status == 401 and not_address):
        return exc if kind == "wrong_key" else tz.FeedError("wrong_key", "wrong API key (HTTP 401 on Prowlarr's "
                                                                      "status endpoint)")
    if not not_address:
        return exc
    if probe.inconclusive:
        failed = probe.failure if isinstance(probe.failure, tz.FeedError) else tz.FeedError("unreachable", "")
        return tz.FeedError(failed.kind, f"Prowlarr's status endpoint failed ({failed}), and t=caps found no "
                                         f"Torznab feed ({exc}); asked again next time",
                            retry_after=failed.retry_after, status=failed.status)
    if probe.status == 404 and getattr(exc, "hint", "") == "json":
        return tz.FeedError("not_address", "not a Torznab or Prowlarr address: this looks like Prowlarr's own /api; "
                                           "paste its base address, e.g. http://host:9696")
    return tz.FeedError("not_address", f"not a Torznab or Prowlarr address: neither Prowlarr's status endpoint nor "
                                    f"t=caps answered as one ({exc})")


def _confirm_torznab(job: tz.Job, src: tz.Source, probe: Probe, path, prior: dict | None) -> None:
    """Detection step 2: `t=caps` on the server's feed (`torznab.caps`, whose answer is cached for the searches
    that follow). A `<caps>` answer makes it a Torznab feed, remembered; a failure is named by `_detected`, and
    a definite one (wrong key, login, not an address) is remembered for `CAPS_MISS_TTL_S` in the server record
    alone (not also as a caps miss), beside the remembered list. A check never turns a good record (`prowlarr`,
    `torznab`) into a refusal except for a wrong key or a login."""
    t0 = time.monotonic()
    try:
        tz.caps(job, src.feed, remember_miss=False)
    except tz.FeedError as exc:
        named = _detected(exc, probe, src)
        keep_good = (job.fresh and (prior or {}).get("kind") in ("prowlarr", "torznab")
                     and named.kind not in ("wrong_key", "login"))
        if named.kind in tz._DEFINITE and not keep_good:
            tz._write(job, path, {"kind": "none", "key": tz._fingerprint(src.key), "ts": time.time(),
                                  "miss": named.kind, "message": src.feed.mask(str(named)), **_carry(prior)},
                      "servers", {"server": src.label})
        job.say("warn", "server", result="FAIL", ms=(time.monotonic() - t0) * 1000, server=src.label,
                kind=named.kind, error=str(named), status_probe=probe.status,
                suggest=tz._SUGGEST.get(named.kind, "the server is skipped for this call"))
        raise named
    tz._write(job, path, {"kind": "torznab", "key": tz._fingerprint(src.key), "ts": time.time(), **_carry(prior)},
              "servers", {"server": src.label})
    job.say("info", "server", result="PASS", ms=(time.monotonic() - t0) * 1000, server=src.label, kind="torznab",
            source="probe", status_probe=probe.status)


def _has_category(capabilities) -> bool:
    """5070 or 2020 anywhere in `capabilities.categories[].subCategories` (a child sits under its
    parent, so a top-level match finds nothing). Walked without recursion, at most `MAX_CATEGORY_NODES`."""
    if not isinstance(capabilities, dict) or not isinstance(capabilities.get("categories"), list):
        return False
    stack, seen = list(capabilities["categories"]), 0
    while stack and seen < MAX_CATEGORY_NODES:
        node = stack.pop()
        seen += 1
        if not isinstance(node, dict):
            continue
        if type(node.get("id")) is int and node["id"] in (tz.CAT_ANIME, tz.CAT_MOVIE):
            return True
        if isinstance(node.get("subCategories"), list):
            stack.extend(node["subCategories"])
    return False


def _filter_indexers(listing) -> tuple[list[dict], dict, list[dict]]:
    """(kept, dropped counts by reason, dropped `{id, name, reason}`) from `/api/v1/indexer`.
    Kept: `enable` is true, `protocol` is `"torrent"`, and 5070 or 2020 is in the category tree; each kept one is
    `{id, name, priority}` and nothing else (its `fields` hold its secrets). Sorted as Prowlarr prefers them:
    lower `priority` first, then by name, so the earlier feed's record wins an infohash two indexers return."""
    kept: list[dict] = []
    counts: dict[str, int] = {}
    detail: list[dict] = []
    for entry in listing if isinstance(listing, list) else []:
        ident = entry.get("id") if isinstance(entry, dict) else None
        name = core.redact_text(str(entry.get("name") or ""))[:80] if isinstance(entry, dict) else ""
        if type(ident) is not int or ident <= 0:
            why = "not_an_indexer"
        elif entry.get("enable") is not True:
            why = "disabled"
        elif entry.get("protocol") != "torrent":
            why = "usenet" if entry.get("protocol") == "usenet" else "not_torrent"
        elif not _has_category(entry.get("capabilities")):
            why = "no_category"
        else:
            priority = entry.get("priority")
            kept.append({"id": ident, "name": name or f"indexer {ident}",
                         "priority": priority if type(priority) is int else 25})
            continue
        counts[why] = counts.get(why, 0) + 1
        detail.append({"id": ident if type(ident) is int else None, "name": name, "reason": why})
    kept.sort(key=lambda i: (i["priority"], i["name"].casefold(), i["id"]))
    return kept, counts, detail


def _read_list(job: tz.Job, src: tz.Source, retries: int = 2) -> list:
    """`GET {base}/api/v1/indexer` with the key header: the JSON list, or a `FeedError`."""
    status, data, headers = _get_json(job, src, "/api/v1/indexer", LIST_MAX_BODY, retries=retries)
    error = tz.auth_error(status, headers, " on the indexer list") or tz.status_error(status, headers,
                                                                                    " on the indexer list")
    if error is not None:
        raise error
    if not isinstance(data, list):
        raise tz.FeedError("bad_list", f"the indexer list is not a JSON list ({type(data).__name__})")
    return data


def _expand(job: tz.Job, src: tz.Source, rec: dict) -> list[tz.Feed]:
    """A detected Prowlarr's indexer feeds: its remembered list when younger than `LIST_TTL_S` (and the call is
    not a check), else the list read now, filtered (`_filter_indexers`) and remembered. When the refresh fails
    for a passing reason (unreachable, a timeout, a server error, a rate limit), a list younger than
    `LIST_STALE_S` still serves, logged as stale. Raises `FeedError` (`no_indexer` when nothing is
    kept). Logs the ids and names kept and the counts dropped, never an indexer's other fields."""
    t0 = time.monotonic()
    have = rec.get("indexers") if isinstance(rec.get("indexers"), list) else None
    age = time.time() - float(rec.get("list_ts") or 0) if have is not None else None
    source = "cache"
    if have is None or age >= LIST_TTL_S or job.fresh:
        try:
            # With a stale list to fall back on, a play asks once; a check keeps the retry.
            listing = _read_list(job, src, retries=1 if have is not None and not job.fresh else 2)
        except tz.FeedError as exc:
            passing = exc.kind in LIST_TRANSIENT or (exc.kind == "http_status" and (exc.status or 0) >= 500)
            if have is None or not passing or age >= LIST_STALE_S or job.fresh:
                job.say("warn", "indexers", result="FAIL", ms=(time.monotonic() - t0) * 1000, server=src.label,
                        kind=exc.kind, error=str(exc), suggest=tz._SUGGEST.get(exc.kind, "the server is skipped"))
                raise
            job.say("warn", "indexers", result="PASS", ms=(time.monotonic() - t0) * 1000, source="stale",
                    server=src.label, age_s=round(age),
                    kept=[{"id": i.get("id"), "name": i.get("name")} for i in have], kind=exc.kind, error=str(exc),
                    suggest=f"Prowlarr's indexer list could not be read again; the one from {round(age / 3600)} h "
                            f"ago is used until it can")
            source = "stale"
        else:
            kept, counts, detail = _filter_indexers(listing)
            have, over = kept[:MAX_INDEXERS], max(0, len(kept) - MAX_INDEXERS)
            if over:                                    # visible to the check and on a warn line
                counts["over_cap"] = over
                detail += [{"id": i["id"], "name": i["name"], "reason": "over_cap"} for i in kept[MAX_INDEXERS:]]
            rec = {**rec, "indexers": have, "list_ts": time.time(), "dropped": counts, "total": len(listing)}
            tz._write(job, _record_path(job.ctx, src), rec, "servers", {"server": src.label})
            job.say("info" if have and not over else "warn", "indexers", result="PASS" if have else "EMPTY",
                    ms=(time.monotonic() - t0) * 1000, source="prowlarr", server=src.label, total=len(listing),
                    kept=[{"id": i["id"], "name": i["name"]} for i in have], dropped=counts,
                    **({"cap": MAX_INDEXERS} if over else {}),
                    suggest=(tz._SUGGEST["no_indexer"] if not have else
                             f"only the first {MAX_INDEXERS} indexers (by priority) are searched; disable the "
                             f"ones you do not need in Prowlarr" if over else ""))
            if detail:
                job.say("debug", "indexers_dropped", server=src.label, dropped=detail[:MAX_INDEXERS])
            source = "prowlarr"
    if source == "cache":
        job.say("debug", "indexers", result="HIT", source="cache", server=src.label, age_s=round(age),
                kept=[{"id": i.get("id"), "name": i.get("name")} for i in have])
    feeds = _indexer_feeds(src, have)
    if not feeds:
        raise tz.FeedError("no_indexer", "no enabled torrent indexer with an anime or movie category in this Prowlarr "
                                      f"(dropped: {rec.get('dropped') or {}})")
    return feeds


def resolve(job: tz.Job, src: tz.Source, queries: list[str], keep, res: tz.FeedResult) -> list[tz.Feed]:
    """One server for one add-on call: what it is (remembered, or detected now: status endpoint, then `t=caps`,
    detection steps 1-3), then either its Torznab feed searched into `res` (returns []) or a Prowlarr's indexer feeds
    returned for the pool to search. A failure is `res.error`, with its one `feed` line written."""
    t0 = time.monotonic()
    path = _record_path(job.ctx, src)
    # The prior record is always read (its list is carried into any new one); its verdict is used only when it
    # is younger than DETECT_TTL_S and the call is not a check.
    prior = _record(path, src)
    rec = None if job.fresh else _record(path, src, DETECT_TTL_S)
    kind = (rec or {}).get("kind")
    age = time.time() - float((rec or {}).get("ts") or 0)
    if kind == "none" and age >= tz.CAPS_MISS_TTL_S:
        rec = kind = None
    if kind == "torznab":
        job.say("debug", "server", result="HIT", server=src.label, kind="torznab", source="cache", age_s=round(age))
        tz.run_feed(job, src.feed, queries, keep, res)
        return []
    if kind == "none":
        def remembered():
            job.say("info", "server", result="SKIP", server=src.label, kind=rec.get("miss"), source="cache",
                    reason="remembered_refusal", age_s=round(age), error=rec.get("message"),
                    suggest=f"this server was refused {round(age)} s ago with this key; asked again after "
                            f"{tz.CAPS_MISS_TTL_S} s, or at once with another key")
            raise tz.FeedError(str(rec.get("miss") or "error"), str(rec.get("message") or ""))
        tz.run_feed(job, src.feed, queries, keep, res, before=remembered)
        return []
    if kind != "prowlarr":
        probe = _probe_status(job, src)
        if not probe.prowlarr:
            tz.run_feed(job, src.feed, queries, keep, res,
                        before=lambda: _confirm_torznab(job, src, probe, path, prior))
            return []
        rec = {"kind": "prowlarr", "key": tz._fingerprint(src.key), "ts": time.time(), "version": probe.version,
               "url_base": probe.url_base, **_carry(prior)}
        tz._write(job, path, rec, "servers", {"server": src.label})
        job.say("info", "server", result="PASS", ms=(time.monotonic() - t0) * 1000, server=src.label,
                kind="prowlarr", source="probe", version=probe.version, url_base=probe.url_base)
    else:
        job.say("debug", "server", result="HIT", server=src.label, kind="prowlarr", source="cache",
                age_s=round(age), version=rec.get("version"), url_base=rec.get("url_base"))
    res.feed = _server_feed(src)
    try:
        return _expand(job, src, rec)
    except tz.FeedError as exc:
        res.error = exc
        tz._finish(job, res.feed, res, t0, [])
        return []




def remembered(ctx, src: tz.Source) -> dict | None:
    """The server's record as it is now, whatever its age (for the check's answer)."""
    return _record(_record_path(ctx, src), src)


def known_feeds(src: tz.Source) -> tuple[list[tz.Feed], str, str]:
    """(feeds, record, label) for the registry, without the network: a remembered Prowlarr's indexer feeds (from
    its remembered list, whatever its age: it is only a count until a play refreshes it), else the server's one
    Torznab feed. `record` says what the count rests on (`prowlarr`, `torznab`, `none`, `absent`, `other_key`
    for a record made with another key, `unreadable` for a corrupt one); `label` names the server in the
    registry's line: its pasted base until it is known to be a Torznab feed."""
    path = _records_dir() / f"{src.record_key}.json"
    if not path.exists():
        return [src.feed], "absent", src.label
    data = core.cached_json(path, None)
    if not isinstance(data, dict):
        return [src.feed], "unreadable", src.label
    if data.get("key") != tz._fingerprint(src.key):
        return [src.feed], "other_key", src.label
    kind = str(data.get("kind") or "unreadable")
    if kind == "prowlarr" and isinstance(data.get("indexers"), list):
        feeds = _indexer_feeds(src, data["indexers"])
        return feeds, kind, src.label
    return [src.feed], kind, src.feed.label if kind == "torznab" else src.label
