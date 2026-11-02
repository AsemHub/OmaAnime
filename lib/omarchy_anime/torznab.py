"""The built-in Torznab add-on.

The public plugin ships no indexer, but it can search the viewer's own Prowlarr or Jackett (or
any Torznab feed): the viewer lists feeds in the servers file (`core.TORZNAB_SERVERS_FILE`, mode 600, in
the plugin's config folder), and the registry (`indexers.load`) then loads this add-on as `torznab`, in the
`indexers` order like a file add-on. With no file, or an empty list, it is not loaded and nothing changes.

Each server is told apart once and remembered: Prowlarr's status endpoint first
(`appName == "Prowlarr"`), then `t=caps`; else "wrong API key", "the server asks for a login", "unreachable"
or "not a Torznab or Prowlarr address". A Prowlarr base URL is expanded into its enabled torrent
indexers with an anime or movie category (`/api/v1/indexer`, cached `LIST_TTL_S`, refreshed by `check`), each
searched through `{base}/{id}/api` with the key in the `X-Api-Key` header; any other server is one feed.

It offers `episode_search` and `text_search` only (Torznab carries no subtitle tracks, file names or show
ids). Per feed, per call:
- `t=caps` first, read lazily and cached for `CAPS_TTL_S` in the add-on's cache folder: a feed whose
  `search` is unavailable, or that offers no anime category, is skipped with a reason.
- Episode search: `t=search&q="{title} {NN}"`, `cat=5070` (+2020 for a movie), `extended=1`, `limit` within
  the feed's `limits`; at most `MAX_EPISODE_QUERIES` per feed, highest value first. A movie is
  asked by title alone (a movie's release is never numbered). The pack pass asks `q="{title}"` and keeps
  packs only (`query.pack_by`: `batch`, a range, or a season pack of the entry's own season), and logs which
  pattern made each one a pack (`packs`). Text search asks the viewer's words once.
- Items become releases by a whitelist of fields; an item without an infohash is dropped and counted, and
  no link is ever fetched (Prowlarr's links carry its API key).
- XML is parsed only after the body is capped (`MAX_BODY`), has no NUL byte and no `<!DOCTYPE`/`<!ENTITY`;
  `ParseError` and `ValueError` are both caught.
- The key goes in the `apikey` query parameter of a plain Torznab feed (Jackett reads only the query)
  and in the `X-Api-Key` header of every call to a detected Prowlarr; every logged URL has it
  masked, a redirect to another origin is refused, and any error text is masked before it is logged or
  raised. No error body of any call is logged; of an `<error>` answer only its code and its
  description's first words are (`error_words`).
- Servers and feeds are asked side by side (at most `MAX_FEED_WORKERS` at once), and the add-on answers before
  its own time budget (`indexerTimeoutSeconds`) runs out: a slow or failing server or feed costs itself only.
"""
from __future__ import annotations

import base64
import heapq
import itertools
import hashlib
import json
import queue
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from types import MappingProxyType, ModuleType

from . import core, indexers, say, score, torznab_servers

NAME = "Torznab"
STEM = "torznab"                      # what the `indexers` setting names it, and its cache folder
COMPONENT = "indexers.torznab"
SERVERS_FILE = torznab_servers.FILE

MAX_BODY = 4 * 2**20                  # one feed answer; ~2000 items of ~2 KB, far past a `limit=100` page
CAPS_TTL_S = 24 * 3600                # a feed's caps, until the Accounts check reads them again
CAPS_MISS_TTL_S = 600                 # a definite refusal (wrong key, login, not Torznab) is not asked again for this long
BACKOFF_MAX_S = 3600                  # the longest `Retry-After` a feed is left alone for across plays
MAX_FEED_WORKERS = 4                  # feeds asked side by side
MAX_EPISODE_QUERIES = 4               # main + best alternative title x AniList + absolute number
MAX_PACK_QUERIES = 2                  # the pack pass: main + best alternative title, no number
MAX_ITEMS = 200                       # items read from one answer (twice the `limit` asked); the rest are counted
# Characters of a title kept (recorded maximum 259), cut after the key scrub has read the whole title: each masked
# copy shortens it, so a cut before the scrub let half a key from past the cut into the kept part.
TITLE_MAX = 500
DROPPED_TITLES_LOGGED = 25            # titles on one query's `dropped` debug line
SINGLE_EPISODE_NAMES_LOGGED = 40      # names on one search's `single_episode` line
FRANCHISE_SHOW_NAMES_LOGGED = 40      # names on one search's `franchise_show` line
PACK_NAMES_LOGGED = 40                # names on one pack pass's `packs` line
PACK_ONLY = "the pack pass keeps packs only"
WANTED_LIMIT = 100                    # items per query, lowered to the feed's `limits max`
CAT_ANIME, CAT_MOVIE = 5070, 2020
ACCEPT = "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.1"
ERROR_WORDS_MAX = 200                 # characters of an `<error>` description a line or message keeps


# ----------------------------------------------------------------------------- servers

@dataclass(frozen=True)
class Feed:
    """One Torznab feed: `url` is the endpoint (its path ends in `/api`), without query or key. `label` is what
    the log shows (host, port and path; never the key, never user info).

    `header` sends the key as `X-Api-Key` instead of the `apikey` query parameter, for the `/{id}/api`
    feeds of a detected Prowlarr (a `?apikey=` wins there even when empty and is copied
    into Prowlarr's URL-base redirect); a plain Torznab feed keeps the query, which is all Jackett reads.
    `server`, `indexer` and `indexer_id` name, on every log line of the feed, the Prowlarr it came from and the
    indexer it searches."""
    url: str
    key: str
    label: str
    name: str = ""
    header: bool = False
    server: str = ""
    indexer: str = ""
    indexer_id: int = 0

    def params_url(self, params: dict, masked: bool) -> str:
        q = urllib.parse.urlencode(params)
        if self.key and not self.header:
            q += "&apikey=" + (core.REDACTED if masked else urllib.parse.quote(self.key, safe=""))
        return f"{self.url}?{q}"

    def headers(self, accept: str) -> dict:
        return {"Accept": accept, **({"X-Api-Key": self.key} if self.key and self.header else {})}

    @property
    def tags(self) -> dict:
        """What each of this feed's log lines names it by: the feed, and for a Prowlarr's indexer the server
        and the indexer."""
        out = {"feed": self.label}
        if self.server:
            out["server"] = self.server
        if self.indexer_id:
            out.update(indexer=self.indexer, indexer_id=self.indexer_id)
        return out

    def mask(self, text: str) -> str:
        """Error or answer text with this feed's key taken out, whatever its length or where it appears."""
        text = str(text)
        if self.key:
            text = text.replace(self.key, core.REDACTED).replace(urllib.parse.quote(self.key, safe=""), core.REDACTED)
        return core.redact_text(text)

    @property
    def key_fingerprint(self) -> str:
        """A short hash of the key, stored with a remembered refusal so a changed key is not refused from the
        cache. 48 bits of SHA-256 of a 32-character key reveal nothing of it."""
        return _fingerprint(self.key)

    @property
    def cache_key(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()[:20]


def _fingerprint(key: str) -> str:
    return hashlib.sha256(("torznab-key:" + key).encode()).hexdigest()[:12]


def feed_endpoint(url: str) -> tuple[str, str]:
    """(feed URL, reason it is refused or ""). The pasted URL minus a trailing `/`; its path gets `/api`
    appended unless it already ends in `/api` (Sonarr's rule; Jackett's `torznab/{ignored?}`
    route accepts it). A key or user info inside the address is refused: the key has its own field."""
    raw = str(url or "").strip()
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in raw):
        return "", "the address holds a space or a control character; give the address alone"
    if re.search(r"(?i)(api_?key|passkey|jackett_apikey)=", raw):
        return "", "the address holds a key; give the address alone, the key in its own field"
    try:
        p = urllib.parse.urlsplit(raw)
        p.port                                          # raises ValueError on a bad port
    except ValueError as exc:
        return "", f"the address is not a URL ({exc})"
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return "", "the address must start with http:// or https:// and name a host"
    if p.username or p.password or "@" in p.netloc:
        return "", "the address holds a user name or password; give the address alone"
    if p.query or p.fragment:
        return "", "the address holds a query (?...); give the address alone, the key in its own field"
    path = p.path.rstrip("/")
    if not path.endswith("/api"):
        path += "/api"
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc, path, "", "")), ""


def feed_label(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.hostname}{':' + str(p.port) if p.port else ''}{p.path}"


@dataclass(frozen=True)
class Source:
    """One server of the file, checked: `base` is the address as pasted minus a trailing `/` (a
    Prowlarr's feeds are `{base}/{id}/api`), `feed` the Torznab feed it is when it is not a
    Prowlarr (`feed_endpoint`), `label` the server as the log names it (host, port and path)."""
    server: torznab_servers.Server
    base: str
    feed: Feed

    @property
    def label(self) -> str:
        return feed_label(self.base)

    @property
    def key(self) -> str:
        return self.server.key

    @property
    def record_key(self) -> str:
        """Names the server's remembered detection (`servers/<this>.json`): a changed address is another
        server; a changed key is caught by the fingerprint inside."""
        return hashlib.sha256(("torznab-server:" + self.base).encode()).hexdigest()[:20]


def source_of(server: torznab_servers.Server) -> tuple[Source | None, str]:
    """The server checked, or (None, why it is refused): the address rules of `feed_endpoint`."""
    url, why = feed_endpoint(server.url)
    if why:
        return None, why
    p = urllib.parse.urlsplit(server.url.strip())
    base = urllib.parse.urlunsplit((p.scheme.lower(), p.netloc, p.path.rstrip("/"), "", ""))
    return Source(server=server, base=base, feed=Feed(url=url, key=server.key, label=feed_label(url),
                                                      name=server.name)), ""


def feeds_of(server: torznab_servers.Server) -> tuple[list[Feed], str]:
    """The feeds one server is known to be searched through now, without asking the network, or ([], why it
    is refused). A server remembered as a Prowlarr is its indexers' feeds, from the remembered list
    (however old: it is only a count until a play refreshes it); any other server, or one not yet detected, is
    its one Torznab feed (`feed_endpoint`). Detection and expansion happen in the add-on's call (`run`),
    where there is an `Http`, a cache folder and a deadline."""
    src, why = source_of(server)
    if src is None:
        return [], why
    return torznab_prowlarr.known_feeds(src)[0], ""


@dataclass
class Loaded:
    """The servers file turned into sources, for the registry: `servers` used, `sources` (one per server, a
    duplicate address dropped), `feeds` the feeds known now (`feeds_of`, for the count), `skipped` one
    `{server, reason}` per entry left out (the file's own refusals and `source_of`'s), `problem` why the
    whole file is unusable, `path` the file."""
    servers: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    feeds: list = field(default_factory=list)
    labels: list = field(default_factory=list)       # one per server: its base, or its feed once known
    records: list = field(default_factory=list)      # per server: what its count rests on (`known_feeds`)
    skipped: list = field(default_factory=list)
    problem: str = ""
    path: str = ""


def load_feeds() -> Loaded:
    """Read the servers file (`torznab_servers.read`) and check each server (`source_of`, `feeds_of`). A file
    whose entries are all refused has a `problem` too. Nothing here logs (the registry does) and nothing asks
    the network."""
    got = torznab_servers.read()
    out = Loaded(problem=got.problem, skipped=list(got.skipped), path=str(torznab_servers.path()))
    seen: set[str] = set()
    numbered = iter(n for n in range(1, 10**6) if all(s["server"] != n for s in got.skipped))
    for server in got.servers:
        n = next(numbered)
        src, why = source_of(server)
        if src is not None and src.feed.url in seen:
            why = "the same feed is listed earlier"
        if why:
            out.skipped.append({"server": n, "reason": why})
            continue
        seen.add(src.feed.url)
        out.servers.append(server)
        out.sources.append(src)
        feeds, record, label = torznab_prowlarr.known_feeds(src)
        out.feeds.extend(feeds)
        out.labels.append(label)
        out.records.append({"server": src.label, "record": record, "feeds": len(feeds)})
    out.skipped.sort(key=lambda s: s["server"])
    if out.skipped and not out.sources and not out.problem:
        out.problem = f"no server in {out.path} can be used: " + "; ".join(
            f"server {s['server']}: {s['reason']}" for s in out.skipped)
    return out


# ----------------------------------------------------------------------------- answers

class FeedError(Exception):
    """One feed call that gave nothing usable. `kind`: wrong_key, login, error, rate_limited, indexer_paused
    (Prowlarr has paused the indexer after its own failed searches), indexer_failed (Prowlarr answered
    500 for one of its indexers: it is up but could not ask that indexer), http_status, unreachable,
    too_large, redirect, unsafe, not_xml, not_torznab. Its text is already masked. `hint` refines a kind without
    changing it: `json` (a `not_xml` that is JSON); for `rate_limited`, `query_limit` (Prowlarr's own 429); for
    `rate_limited` or `indexer_paused`, `backoff` (a pause remembered from an earlier call), whose `cause`
    is the reason the pause was remembered for (one of `_BACKOFF_CAUSES`) and `stored_cause` the name the record
    held when it was renamed since (`_RENAMED_CAUSES`), else ""."""

    def __init__(self, kind: str, message: str, retry_after: int | None = None, status: int | None = None,
                 hint: str = "", cause: str = "", stored_cause: str = ""):
        super().__init__(message)
        self.kind, self.retry_after, self.status, self.hint = kind, retry_after, status, hint
        self.cause, self.stored_cause = cause, stored_cause


def safe_xml(body) -> ET.Element:
    """Parse one answer: capped, no NUL byte (UTF-16/32), no DTD or entity declaration, then
    `ET.fromstring`, whose `ParseError` and `ValueError` (a declared multi-byte encoding) both become a
    `FeedError`. External entities and DTDs are never loaded by expat on this Python."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not isinstance(body, (bytes, bytearray)):
        raise FeedError("not_xml", f"the answer is {type(body).__name__}, not XML")
    if len(body) > MAX_BODY:
        raise FeedError("too_large", f"the answer is over {MAX_BODY} bytes")
    if b"\x00" in body:
        raise FeedError("unsafe", "the answer holds a NUL byte (UTF-16/32 or binary); refused before parsing")
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise FeedError("unsafe", "the answer declares a DTD or an entity; refused before parsing")
    try:
        return ET.fromstring(bytes(body))
    except (ET.ParseError, ValueError) as exc:
        raise FeedError("not_xml", f"the answer is not XML ({type(exc).__name__}: {str(exc)[:120]})")


def _header(headers: dict, name: str):
    return next((v for k, v in (headers or {}).items() if str(k).lower() == name.lower()), None)


def _retry_after(headers: dict) -> int | None:
    raw = str(_header(headers, "Retry-After") or "").strip()
    return int(raw) if raw.isascii() and raw.isdigit() else None        # `int("²")` raises


def auth_error(status: int, headers: dict, where: str) -> FeedError | None:
    """A 401's meaning: with `WWW-Authenticate` something in front of the server wants a
    login; without it (Prowlarr's empty 401) the key was refused. `where` names the call (" on the indexer list")."""
    if status != 401:
        return None
    if _header(headers, "WWW-Authenticate"):
        return FeedError("login", f"the server asks for a login (HTTP 401 with WWW-Authenticate){where}: something "
                                  f"in front of it wants a user name and password")
    return FeedError("wrong_key", f"wrong API key (HTTP 401{where})")


def status_error(status: int, headers: dict, where: str) -> FeedError | None:
    """A 429 (with its `Retry-After`) or any other status than 200, as a `FeedError`; None for a 200."""
    if status == 429:
        return FeedError("rate_limited", f"rate limited (HTTP 429){where}", _retry_after(headers))
    if status != 200:
        return FeedError("http_status", f"HTTP {status}{where}", status=status)
    return None


def _why_429(desc: str) -> str:
    """What a Torznab `<error code="429">` is, from Prowlarr's own words
    (`NewznabController.cs`): `indexer_paused` when Prowlarr has paused the indexer after its own failed searches
    (`Indexer is disabled till … due to recent failures.`), `query_limit` when a limit set in Prowlarr is reached,
    else "" (a plain rate limit)."""
    d = desc.lower()
    if "due to recent failures" in d:
        return "indexer_paused"
    if "query limit" in d:
        return "query_limit"
    return ""


# A local path in an error's words (`/home/u/.config/x`, `~/x/y`, `C:\x\y`): at least two separators,
# never one inside a URL (`https://host/a/b`: the character before each `/` is `:`, `/` or the host's own).
_LOCAL_PATH = re.compile(r"(?<![\w:/\\.-])(?:~|[A-Za-z]:)?[/\\](?:[^\s/\\'\"<>(),;]+[/\\])+[^\s/\\'\"<>(),;]*")
# A quoted path whole, spaces included. .NET quotes the path in its IO exceptions' messages ("Could not
# find file '{0}'.", "Access to the path '{0}' is denied.": dotnet/runtime `Strings.resx`), which is what Jackett's
# code 900 first line holds when a file fails. Also a UNC path (`\\host\share\x`) and a `file://` address, unquoted.
_QUOTED_PATH = re.compile(r"""(['"])(?:~?/|[A-Za-z]:[\\/]|\\\\|(?i:file)://)[^'"\r\n]*\1""")
_UNC_PATH = re.compile(r"(?<![\w\\])\\\\[^\s/\\'\"<>(),;]+\\[^\s'\"<>(),;]+")
_FILE_URL = re.compile(r"(?i)(?<![\w.+-])file://[^\s'\"<>]*")
ERROR_CODE_DIGITS = 5                 # an `<error>` code is shown when it is a number of at most this many digits
# Where .NET's exception text turns into its inner exceptions or its stack (`ex.ToString()`), on one line too: an XML
# attribute's raw newline reaches the parser as a space.
_STACK = re.compile(r"[\r\n]|\s*---> |\s{2,}at\s")


def error_words(text: str, mask) -> str:
    """What of a server's `<error>` description a log line or a message may carry: the text before its
    first line break, nested exception (` ---> `) or stack frame (`   at `), local paths as `<path>` (a quoted one
    whole, a UNC path, a `file://` address, else up to its first space), masked by `mask`, at most `ERROR_WORDS_MAX`
    characters. Jackett answers any failed search with the exception's whole text as the description
    (`ResultsController.cs`, code 900); Prowlarr's own are one sentence."""
    first = _STACK.split(str(text or ""), maxsplit=1)[0]
    first = _QUOTED_PATH.sub(lambda m: f"{m.group(1)}<path>{m.group(1)}", first)
    first = _LOCAL_PATH.sub("<path>", _FILE_URL.sub("<path>", _UNC_PATH.sub("<path>", first)))
    return mask(first).strip()[:ERROR_WORDS_MAX]


def error_code(code: str) -> str:
    """An `<error>`'s code as a message shows it: the attribute when it is a number of at most
    `ERROR_CODE_DIGITS` ASCII digits (Prowlarr's and Jackett's are), else "?". The decisions read the raw value."""
    return code if code.isascii() and code.isdigit() and len(code) <= ERROR_CODE_DIGITS else "?"


def check_answer(feed: Feed, status: int, body, headers: dict) -> ET.Element:
    """The answer's root element, or a `FeedError` naming what went wrong: a 401
    without `WWW-Authenticate` is a wrong key, with it a login in front of the server; an `<error>` body is an
    error whatever the status (Jackett sends 200), code 100 a wrong key; 429 is a rate limit, or a paused indexer
    when Prowlarr's words say so. A 500 for a Prowlarr indexer is Prowlarr's own failure to ask it
    (its error pipeline answers an unhandled exception that way). The description is read whole for the
    decision; its message carries `error_words` only."""
    error = auth_error(status, headers, "")
    if error is not None:
        raise error
    root = None
    if body:
        try:
            root = safe_xml(body)
        except FeedError:
            if status == 200 and isinstance(body, (bytes, bytearray)) and bytes(body).lstrip()[:1] in (b"{", b"["):
                # Prowlarr's own `/api` answers JSON: detection names that case in its own words
                raise FeedError("not_xml", "the answer is JSON, not Torznab XML", hint="json")
            if status == 200:
                raise
    if root is not None and root.tag == "error":
        code, raw = str(root.get("code") or ""), str(root.get("description") or "")
        desc, shown = error_words(raw, feed.mask), error_code(code)
        if code == "100":
            raise FeedError("wrong_key", f"wrong API key (<error code=\"100\"> {desc})")
        if status == 429 or code == "429":
            why = _why_429(raw)
            if why == "indexer_paused":
                raise FeedError(why, f"paused by Prowlarr (<error code=\"{shown}\"> {desc})", _retry_after(headers))
            raise FeedError("rate_limited", f"rate limited (<error code=\"{shown}\"> {desc})", _retry_after(headers),
                            hint=why)
        raise FeedError("error", f"the server answered an error (code {shown}: {desc}, HTTP {status})")
    if feed.indexer_id and status == 500:
        raise FeedError("indexer_failed", f"Prowlarr could not search this indexer (HTTP {status})", status=status)
    error = status_error(status, headers, "")
    if error is not None:
        raise error
    if root is None:
        raise FeedError("not_xml", "the answer is empty")
    return root


def read_caps(root: ET.Element) -> dict:
    """`{search, categories, limit_max, limit_default}` from a `<caps>` answer. A missing `search` element
    counts as available (the spec's default); every `category`/`subcat` id is an offered category."""
    if root.tag != "caps":
        tag = re.sub(r"[^\w:.-]", "?", root.tag.rsplit("}", 1)[-1])[:40]      # the server's own text: cut
        raise FeedError("not_torznab", f"the caps answer is <{tag}>, not <caps>: not a Torznab feed")
    search = root.find("searching/search")
    available = search is None or str(search.get("available") or "yes").strip().lower() == "yes"
    cats = sorted({int(c.get("id")) for c in root.iter() if c.tag in ("category", "subcat")
                   and str(c.get("id") or "").isdigit()})
    limits = root.find("limits")

    def num(name):
        v = str(limits.get(name) or "") if limits is not None else ""
        return int(v) if v.isdigit() else None
    return {"search": available, "categories": cats, "limit_max": num("max"), "limit_default": num("default")}


# ----------------------------------------------------------------------------- items

_HEX40 = re.compile(r"[0-9a-fA-F]{40}")
_B32 = re.compile(r"[A-Za-z2-7]{32}")


def normalize_infohash(value) -> str | None:
    """40 lower-case hex, from hex in any case or a 32-character base32 btih; None for anything else."""
    v = str(value or "").strip()
    if _HEX40.fullmatch(v):
        return v.lower()
    if _B32.fullmatch(v):
        return base64.b32decode(v.upper()).hex()
    return None


def infohash_from_magnet(magnet) -> str | None:
    """The btih of a `magnet:?` URI, normalised; None for anything else. The whitespace around it is
    stripped (a re-indented feed puts the field's text on its own line), as `_release_id` strips the guid."""
    m = str(magnet or "").strip()
    if not m.lower().startswith("magnet:?"):
        return None
    for k, v in urllib.parse.parse_qsl(m[len("magnet:?"):], keep_blank_values=True):
        if k.lower() == "xt" and v.lower().startswith("urn:btih:"):
            h = normalize_infohash(v[len("urn:btih:"):])
            if h:
                return h
    return None


def _attrs(item: ET.Element) -> dict[str, list[str]]:
    """Every `torznab:attr`/`newznab:attr` of an item as name -> values (a name can repeat)."""
    out: dict[str, list[str]] = {}
    for el in item:
        if el.tag.rsplit("}", 1)[-1] == "attr" and el.get("name"):
            out.setdefault(el.get("name").strip().lower(), []).append(str(el.get("value") or "").strip())
    return out


# A number attribute as the plugin reads it: ASCII digits, a sign allowed, at most `core.NUMBER_DIGITS_MAX` of them.
# A longer one raised out of the feed's query (`int()` refuses 4,300 digits).
_INT_TEXT = re.compile(r"-?[0-9]{1,%d}" % core.NUMBER_DIGITS_MAX)


def _int(value) -> int | None:
    v = str(value or "").strip()
    return int(v) if _INT_TEXT.fullmatch(v) else None


def _release_id(guid: str, info_hash: str) -> str:
    """The item's `guid` (the spec's per-indexer release id), unless it carries a query or user info (a
    tracker's guid can be a download link with its passkey): then its address without them, plus a short hash
    of the whole, so two such guids stay apart. No guid: the infohash."""
    g = guid.strip()
    if not g:
        return info_hash
    p = urllib.parse.urlsplit(g)
    if p.query or p.username or p.password:
        digest = hashlib.sha256(g.encode()).hexdigest()[:12]
        return urllib.parse.urlunsplit((p.scheme, p.hostname or "", p.path, "", "")) + "#" + digest
    return g


def _scheme(value) -> str:
    """`http`, `magnet`, `other` or `none`: the shape of a link field, never its value (it can hold a passkey)."""
    v = str(value or "").strip().lower()
    return ("none" if not v else "magnet" if v.startswith("magnet:") else
            "http" if v.startswith(("http://", "https://")) else "other")


def link_shapes(item: ET.Element) -> dict[str, str]:
    """The scheme of an item's `link`, `enclosure` and `guid`, for a `no_infohash` item's
    debug line, so a trace can tell a feed's item shape without the raw XML."""
    enclosure = item.find("enclosure")
    return {"link": _scheme(item.findtext("link")),
            "enclosure": _scheme(enclosure.get("url") if enclosure is not None else None),
            "guid": _scheme(item.findtext("guid"))}


def to_release(item: ET.Element, feed: Feed) -> tuple[dict | None, str, str]:
    """(release, "", hash_field) or (None, why, ""): `why` is `no_title` or `no_infohash`, `hash_field` the one that
    gave the hash. Only these fields leave the item; its `link`, `enclosure` and `guid` are read only when
    they are a magnet, and never fetched.

    `guid` is read last, when no other field gave a hash. A Prowlarr indexer that sets its `Guid`
    to the magnet and no `InfoHash` has its
    `link`/`enclosure` pointing at Prowlarr's `/download` proxy, so its magnet reaches us only there. A guid that is
    not a magnet with a valid btih gives nothing."""
    title = (item.findtext("title") or "").strip()
    if not title:
        return None, "no_title", ""
    a = _attrs(item)
    first = lambda name: (a.get(name) or [None])[0]      # noqa: E731
    enclosure = item.find("enclosure")
    hashes = (("infohash", normalize_infohash(first("infohash"))),
              ("magneturl", infohash_from_magnet(first("magneturl"))),
              ("link", infohash_from_magnet(item.findtext("link"))),
              ("enclosure", infohash_from_magnet(enclosure.get("url") if enclosure is not None else None)),
              ("guid", infohash_from_magnet(item.findtext("guid"))))
    hash_field, info_hash = next(((f, v) for f, v in hashes if v), ("", None))
    if not info_hash:
        return None, "no_infohash", ""
    seeders, leechers, peers = _int(first("seeders")), _int(first("leechers")), _int(first("peers"))
    if leechers is None and peers is not None and seeders is not None:
        leechers = max(0, peers - seeders)
    timestamp = None
    try:
        pub = item.findtext("pubDate")
        timestamp = int(parsedate_to_datetime(pub).timestamp()) if pub else None
    except (TypeError, ValueError, IndexError, OverflowError):
        timestamp = None
    return {"id": feed.mask(_release_id(item.findtext("guid") or "", info_hash)),
            "title": feed.mask(title)[:TITLE_MAX],
            "info_hash": info_hash, "magnet": "", "seeders": seeders, "leechers": leechers,
            "total_size": _int(first("size")) or _int(item.findtext("size")),
            "num_files": _int(first("files")) or _int(item.findtext("files")), "timestamp": timestamp,
            "trusted": "trusted" in [t.lower() for t in a.get("tag", [])], "remake": False,
            "source": STEM, "feed": feed.label}, "", hash_field


# ----------------------------------------------------------------------------- the query plan

def _norm(title: str) -> str:
    return re.sub(r"[\W_]+", "", title.casefold())


def _latin(title: str) -> bool:
    """Most of its letters are ASCII: a title an English-language index can match (not the native script)."""
    letters = [c for c in title if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) >= len(letters) / 2


def search_titles(query) -> list[str]:
    """The titles to ask by, best first: the main title (the first of `variants`, else `series_title`), then
    each other Latin-script variant that differs from those already kept."""
    out: list[str] = []
    for t in [*(getattr(query, "variants", None) or []), getattr(query, "series_title", "") or ""]:
        t = str(t or "").strip()
        if t and _latin(t) and _norm(t) and all(_norm(t) != _norm(o) for o in out):
            out.append(t)
    return out


def episode_plan(query, movie: bool) -> tuple[list[str], list[str]]:
    """(queries to send, best first; queries the plan leaves out, for the log). The main title and
    the best alternative title, times the AniList number and the absolute number when it differs
    (`max(alt_numbers)`): at most `MAX_EPISODE_QUERIES`. Further titles are never asked; they are what the
    log lists as skipped. A movie is asked by those two titles alone (its release is never numbered), and so
    is the pack pass (`MAX_PACK_QUERIES`) and a one-episode entry (its releases say `S04E30`, `Special 2` or
    nothing, and a feed that matches every word answered `<title> 01` with nothing)."""
    titles = search_titles(query)
    head, rest = titles[:2], titles[2:]
    if movie or query.pack_pass or getattr(query, "single_episode", False):
        return head, rest                           # two titles at most: within MAX_PACK_QUERIES and the cap
    ep = int(query.episode)
    alts = [int(n) for n in (query.alt_numbers or []) if isinstance(n, int) and n != ep]
    numbers = [ep] + ([max(alts)] if alts else [])
    wanted = [f"{t} {n:02d}" for n in numbers for t in head]
    return wanted[:MAX_EPISODE_QUERIES], wanted[MAX_EPISODE_QUERIES:] + [f"{t} {n:02d}" for t in rest for n in numbers]


# ----------------------------------------------------------------------------- one feed

@dataclass
class FeedResult:
    feed: Feed
    releases: list = field(default_factory=list)
    rejected: list = field(default_factory=list)      # (release, code, why) for the picker
    counts: dict = field(default_factory=dict)
    error: FeedError | None = None
    skipped: list = field(default_factory=list)       # queries the deadline left out
    logged: bool = False                              # its one `feed` line is written


class Job:
    """What every feed of one add-on call shares: the context, the categories, the deadline, and one lock.

    The lock (also `run`'s condition) guards each feed's results, every log line the feeds write and every
    cache write. At the deadline `run` sets `closed` under it: from then on a feed still running writes no line,
    no result and no cache file, so the trace has one `feed` line per feed and nothing after the call returned."""

    def __init__(self, ctx, deadline: float, cats: list[int], about: dict):
        self.ctx, self.log, self.deadline, self.cats, self.about = ctx, ctx.log, deadline, cats, about
        self.caps_timeout = float((ctx.settings or {}).get("httpTimeout") or core.DEFAULT_SETTINGS["httpTimeout"])
        self.lock = threading.Lock()
        self.closed = False
        self.fresh = False              # the Accounts check asks again whatever the cache holds
        self.refreshed: set[str] = set()   # feeds whose caps a check has read already

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def say(self, level: str, stage: str, **kw) -> bool:
        """One log line, unless the call is over (`closed`). True when it was written."""
        with self.lock:
            if self.closed:
                return False
            getattr(self.log, level)(COMPONENT, stage, **kw)
            return True


def cache_root():
    """The add-on's cache folder without a context (the registry's count): what `ctx.cache_dir` is inside a call."""
    return core.CACHE_DIR / indexers.ADDON_CACHE / STEM


def _cache_file(ctx, folder: str, feed: Feed):
    """`<add-on cache>/<folder>/<feed hash>.json`, or None when the cache folder cannot be made (logged by
    `ctx.cache_dir`): the feed is then asked without a cache."""
    return cache_path(ctx, folder, feed.cache_key)


def cache_path(ctx, folder: str, name: str):
    """`<add-on cache>/<folder>/<name>.json` inside a call, or None when the folder cannot be made."""
    try:
        return ctx.cache_dir / folder / f"{name}.json"
    except OSError:
        return None


def _backoff(job: Job, feed: Feed) -> tuple[float | None, str, str]:
    """(until, cause, stored) of the feed's remembered pause, or (None, "", ""): `cause` is the skip reason the pause
    was remembered for, one of `_BACKOFF_CAUSES`; "rate_limited" for a record written before it was kept or
    holding anything else (never `backoff` itself); `indexer_paused` for one written as
    `indexer_disabled`, the code's earlier name, and then `stored` is that old name (else "")."""
    path = _cache_file(job.ctx, "backoff", feed)
    data = core.cached_json(path, BACKOFF_MAX_S) if path is not None else None
    data = data if isinstance(data, dict) else {}
    until = data.get("until")
    if not (isinstance(until, (int, float)) and until > time.time()):
        return None, "", ""
    stored = data.get("cause")
    renamed = _RENAMED_CAUSES.get(stored) if isinstance(stored, str) else None
    cause = renamed or stored
    return until, cause if cause in _BACKOFF_CAUSES else "rate_limited", stored if renamed else ""


def _backoff_until(job: Job, feed: Feed) -> float | None:
    return _backoff(job, feed)[0]


def _remember(job: Job, folder: str, feed: Feed, value: dict) -> None:
    """Write one cache file, unless the call is over: an answer that came after the deadline is not kept."""
    _write(job, _cache_file(job.ctx, folder, feed), value, folder, feed.tags)


def _write(job: Job, path, value: dict, what: str, tags: dict) -> None:
    """Write one cache file under the call's lock: the deadline cannot close the call while a
    write is under way, so no file lands after the call returned."""
    if path is None:
        return
    failed = None
    with job.lock:
        if job.closed:
            return
        try:
            core.write_cache(path, value)
        except OSError as exc:
            failed = exc
    if failed is not None:
        exc = failed
        job.say("warn", "cache", result="FAIL", **tags, what=what,
                error=f"{type(exc).__name__}: {exc}", suggest="the add-on's cache folder could not be written; "
                "the feed is asked again next time")


def _send(job: Job, url: str, headers: dict, *, log_url: str, cap: int, quick: bool, retries: int, mask,
          expected: tuple[int, ...] = ()) -> tuple[int, object, dict]:
    """One GET through the plugin's `Http` under the job's deadline, for a feed and for Prowlarr's JSON API alike:
    capped (`cap`, on the success and the error body), raw bodies, same-origin redirects only, the key masked in
    the logged URL (`log_url`), an error body never logged, only its length (`log_body=False`: Prowlarr's
    500 carries .NET stack text with its local paths, Jackett's `<error>` the exception's whole text). A `quick`
    call (caps, detection, the indexer list) keeps `httpTimeout` and gets up to `retries` attempts only when the
    time left holds two; a search gets all the time left, sent once.
    Every call ends before the deadline (its socket timeout is the time left, less a little, so `Http`'s own
    line is written inside the call). Raises `FeedError` (masked by `mask`) when nothing came back; a search
    that ran out of time is a `timeout`, not "unreachable"."""
    left = job.left()
    if left <= 0.45:
        raise FeedError("timeout", "no time left in the add-on's budget")
    room = left - 0.25
    if quick:
        timeout = min(job.caps_timeout, room)
        retries = retries if room > 2 * timeout + 3.0 else 1
    else:
        timeout, retries = room, 1
    try:
        return job.ctx.http.request(
            COMPONENT, "GET", url, headers=headers, expect_json=False, raw_body=True, max_bytes=cap,
            same_origin=True, retries=retries, log_url=log_url, timeout=max(0.2, timeout), expected=expected,
            log_body=False,
            max_retry_after=max(0.0, min(core.RETRY_AFTER_CAP_S, room - timeout - 1.0)))
    except core.StageError as exc:
        reason = exc.data.get("reason")
        kind = ("timeout" if reason == "answer_lost" and exc.data.get("timed_out") else
                {"too_large": "too_large", "cross_origin_redirect": "redirect"}.get(reason, "unreachable"))
        raise FeedError(kind, mask(str(exc)))


def fetch(job: Job, feed: Feed, params: dict) -> ET.Element:
    """One GET of the feed, through the plugin's `Http` (one log line with the key masked), capped, same-origin
    only, and checked (`check_answer`). Every call ends before the deadline (its socket timeout is the time
    left, less a little, so `Http`'s own line is written inside the call):
    - a search may take all of it, not `httpTimeout`: Jackett's `all` waits up to 40 s on its own indexers.
      It is sent once: a retry would only ask those indexers again;
    - `t=caps` answers at once on every server read, so it keeps `httpTimeout`, and gets `Http`'s retry (and a
      short `Retry-After` wait) only when the time left holds two attempts.
    A search that ran out of time is a `timeout`, not "unreachable"."""
    quick = params.get("t") == "caps"
    status, body, headers = _send(job, feed.params_url(params, masked=False), feed.headers(ACCEPT),
                                  log_url=feed.params_url(params, masked=True), cap=MAX_BODY, quick=quick,
                                  retries=2 if quick else 1, mask=feed.mask)
    return check_answer(feed, status, body, headers)


# Refusals that will not change by asking again soon: remembered for `CAPS_MISS_TTL_S`, tied to a fingerprint of
# the key they were given with, so a corrected key is tried at once. A one-off `<error>`, a page that is not XML
# or an unsafe answer is not remembered: the next play asks again (a graduated answer, not a 10 min penalty).
_DEFINITE = ("wrong_key", "login", "not_torznab", "not_address")


def caps(job: Job, feed: Feed, remember_miss: bool = True) -> dict:
    """The feed's caps: from the cache when younger than `CAPS_TTL_S` (a remembered refusal when younger than
    `CAPS_MISS_TTL_S`), else `t=caps` now. Raises `FeedError`. `remember_miss=False` for detection's step 2,
    whose refusal the server record keeps (one store per refusal)."""
    t0 = time.monotonic()
    path = _cache_file(job.ctx, "caps", feed)
    fresh = job.fresh and feed.url not in job.refreshed      # a check asks each feed once, then uses that
    cached = core.cached_json(path, CAPS_TTL_S) if path is not None and not fresh else None
    if isinstance(cached, dict):
        age = time.time() - float(cached.get("ts") or 0)
        if cached.get("miss") and age < CAPS_MISS_TTL_S and cached.get("key") == feed.key_fingerprint:
            job.say("info", "caps", result="SKIP", **feed.tags, source="cache", age_s=round(age),
                         kind=cached["miss"], reason="remembered_refusal", error=cached.get("message"),
                         suggest=f"this feed refused the caps call {round(age)} s ago; asked again after "
                                 f"{CAPS_MISS_TTL_S} s")
            raise FeedError(cached["miss"], str(cached.get("message") or cached["miss"]))
        if not cached.get("miss") and isinstance(cached.get("caps"), dict):
            job.say("debug", "caps", result="HIT", **feed.tags, age_s=round(age), **cached["caps"])
            return cached["caps"]
    try:
        try:
            got = read_caps(fetch(job, feed, {"t": "caps"}))
        except FeedError as raw:                        # masked before it is logged or remembered
            raise FeedError(raw.kind, feed.mask(str(raw)), raw.retry_after, raw.status, raw.hint) from None
    except FeedError as exc:
        if exc.kind in _DEFINITE and remember_miss:
            _remember(job, "caps", feed, {"miss": exc.kind, "message": str(exc), "ts": time.time(),
                                          "key": feed.key_fingerprint})
        job.say("warn", "caps", result="FAIL", ms=(time.monotonic() - t0) * 1000, **feed.tags,
                     kind=exc.kind, error=str(exc), suggest=_SUGGEST.get(exc.kind, "the feed is skipped for this call"))
        raise
    _remember(job, "caps", feed, {"caps": got, "ts": time.time()})
    job.refreshed.add(feed.url)
    job.say("info", "caps", result="PASS", ms=(time.monotonic() - t0) * 1000, **feed.tags,
                 source="feed", **{k: got[k] for k in ("search", "limit_max", "limit_default")},
                 categories=len(got["categories"]), anime=CAT_ANIME in got["categories"])
    return got


_SUGGEST = {
    "wrong_key": "check the API key: Prowlarr shows it under Settings > General, Jackett at the top right of its "
                 "dashboard",
    "login": "a proxy or login page stands in front of the server; give the address the server itself answers on",
    "rate_limited": "the server asked for a pause; the feed is left alone until then",
    "indexer_paused": "Prowlarr has paused this indexer after its own searches failed; Prowlarr's System > Status "
                      "lists it and Prowlarr's own log says why (e.g. Cloudflare); the feed is left alone until then",
    "query_limit": "the query limit set for this indexer in Prowlarr is reached; the feed is left alone until then",
    "indexer_failed": "Prowlarr answered but could not search this indexer (its site did not answer Prowlarr, say); "
                      "Prowlarr's own log says why (System > Events); the feed is skipped for this call",
    "backoff": "remembered from an earlier call, no request is sent until the pause ends",
    "unreachable": "check that the server is running and reachable from this machine",
    "timeout": "raise indexerTimeoutSeconds, or remove a slow feed",
    "not_torznab": "the address is not a Torznab feed; for Jackett use the Torznab feed URL, for Prowlarr "
                   "Prowlarr's own address (e.g. http://host:9696)",
    "redirect": "the server redirects to another host or port; give the address it redirects to instead "
                "(e.g. its https:// address)",
    "unsafe": "the feed answered something that is not a plain Torznab XML answer",
    "too_large": "the feed answered more than the add-on reads; lower its results limit",
    "not_address": "give Prowlarr's own address as the browser shows it (e.g. http://host:9696), or a Jackett or "
                   "other Torznab feed URL",
    "no_indexer": "in Prowlarr, enable a torrent indexer that offers TV/Anime (5070) or Movies/Other (2020)",
    "bad_list": "Prowlarr's indexer list could not be read; check the address and that it is Prowlarr's own",
}


# Every SKIP line names one of these as `reason`, a code a trace can be grouped by (its prose is in `why` or
# `error`). A feed's failure of these kinds is a SKIP (the feed was left alone), any other a FAIL or PARTIAL.
_SKIP_KINDS = ("no_search", "no_category", "rate_limited", "indexer_paused", "no_indexer")
_PAUSE_KINDS = ("rate_limited", "indexer_paused")           # a failure whose Retry-After is remembered (`backoff/`)
_BACKOFF_CAUSES = ("rate_limited", "indexer_paused", "query_limit")      # what a remembered pause can be for
_RENAMED_CAUSES = {"indexer_disabled": "indexer_paused"}  # a stored cause by its earlier name
_SKIP_REASONS = ("no_search", "no_category", "no_indexer", "rate_limited", "indexer_paused", "query_limit",
                 "backoff", "remembered_refusal", "duplicate", "not_started", "deadline", "movie_no_packs")


# The `error` of a remembered pause, by the cause it was remembered for (the words of the first answer,
# not "the server asked", on every later line of the pause).
_PAUSED = {"rate_limited": "the server asked for a pause until {} s from now",
           "indexer_paused": "Prowlarr has paused this indexer after its own failed searches, for {} s more",
           "query_limit": "the query limit set in Prowlarr for this indexer is reached, for {} s more"}


def skip_reason(error: FeedError) -> str:
    """The `reason` of a skipped feed's line: its kind, except a query-limit 429 (`_why_429`) and a pause
    remembered from an earlier call (`backoff`, no request sent)."""
    if error.kind in _PAUSE_KINDS and error.hint in ("backoff", "query_limit"):
        return error.hint
    return error.kind


def new_result(feed: Feed) -> FeedResult:
    return FeedResult(feed=feed, counts={"items": 0, "kept": 0, "no_infohash": 0, "no_title": 0, "filtered": 0})


def run_feed(job: Job, feed: Feed, queries: list[str], keep, res: FeedResult, before=None) -> FeedResult:
    """One feed for one add-on call, into `res`: back-off, caps, categories, then each query in order while time
    is left. `keep(release) -> (bool, code, why)` is the filter; a release it rejects with code `series`/`season`
    goes to the picker's rejected list, others are only counted. Each finished query's releases join `res` at
    once (under the job's lock), so a later query cut off at the deadline costs only itself.
    `before()` runs after the back-off check and may raise a `FeedError` (detection step 2 of a server not
    yet known, `_confirm_torznab`); the feed's one `feed` line then names that failure."""
    t0 = time.monotonic()
    try:
        until, cause, stored = _backoff(job, feed)
        if until is not None:
            left = round(until - time.time())
            # The pause's own kind, so every line of a paused indexer's pause reads as one
            raise FeedError(cause if cause in _PAUSE_KINDS else "rate_limited",
                            _PAUSED.get(cause, _PAUSED["rate_limited"]).format(left) +
                            " (remembered from an earlier call)", left, hint="backoff", cause=cause,
                            stored_cause=stored)
        if before is not None:
            before()
        got = caps(job, feed)
        if not got["search"]:
            raise FeedError("no_search", "the feed's caps say search is unavailable")
        offered = set(got["categories"])
        cats = [c for c in job.cats if c in offered] if offered else list(job.cats)
        if not cats:
            raise FeedError("no_category", f"the feed offers none of the categories {job.cats} (anime is 5070)")
        limit = min(WANTED_LIMIT, got["limit_max"]) if got["limit_max"] else WANTED_LIMIT
        seen: set[str] = set()
        for n, q in enumerate(queries):
            if job.left() <= 0.45:
                res.skipped = queries[n:]
                job.say("info", "query", result="SKIP", **feed.tags, skipped=res.skipped, reason="deadline",
                        why="the add-on's time budget is nearly spent", **job.about)
                break
            tq = time.monotonic()
            root = fetch(job, feed, {"t": "search", "q": q, "cat": ",".join(map(str, cats)), "extended": 1,
                                     "limit": limit})
            if root.tag != "rss":
                raise FeedError("not_torznab", f"the search answer is <{root.tag.rsplit('}', 1)[-1][:40]}>, not "
                                               f"<rss>: not a Torznab answer (a login or error page?)")
            items = list(root.iter("item"))
            over_cap = max(0, len(items) - MAX_ITEMS)       # a feed that ignores `limit`, or a hostile one
            kept = unnumbered = 0
            dropped: dict[str, int] = {}
            titles: list = []                           # (code, title[, shapes]) of each dropped item (debug line)
            found: list = []
            rejected: list = []
            counts = {"no_infohash": 0, "no_title": 0, "filtered": 0}
            hash_from: dict[str, int] = {}              # which item field gave each release's hash
            cut: list[int] = []                         # the length of each title over TITLE_MAX
            for item in items[:MAX_ITEMS]:
                raw_title = (item.findtext("title") or "").strip()
                if len(raw_title) > TITLE_MAX:
                    cut.append(len(raw_title))
                rel, why, hash_field = to_release(item, feed)
                if rel is None:
                    counts[why] += 1
                    if why == "no_infohash":               # which item, and its fields' shapes
                        titles.append([why, feed.mask(raw_title)[:TITLE_MAX], link_shapes(item)])
                    continue
                hash_from[hash_field] = hash_from.get(hash_field, 0) + 1
                if rel["info_hash"] in seen:
                    continue
                seen.add(rel["info_hash"])
                ok, code, reason = keep(rel)
                if not ok:
                    counts["filtered"] += 1
                    dropped[code] = dropped.get(code, 0) + 1
                    # A title stating the entry's season but not read as its pack says why.
                    # An `episode` drop says how its name was read (`score.title_episode_log`).
                    if (code == "pack" and reason != PACK_ONLY) or code == score.FRANCHISE_SHOW:
                        titles.append([code, rel["title"], reason])
                    elif code == "episode":
                        titles.append([code, rel["title"], score.title_episode_log(rel["title"])])
                    else:
                        titles.append([code, rel["title"]])
                    # A `franchise_show` drop goes back too, so a viewer's pick of it can be
                    # found again; the plugin keeps it off the picker's rows.
                    if code in ("series", "season", score.FRANCHISE_SHOW):
                        rejected.append((rel, code, reason))
                    continue
                kept += 1
                unnumbered += code == "unnumbered"
                found.append(rel)
            with job.lock:
                if job.closed:
                    return res
                res.releases.extend(found)
                res.rejected.extend(rejected)
                res.counts["items"] += len(items)
                for k, v in counts.items():
                    res.counts[k] += v
                res.counts["kept"] = len(res.releases)
            job.say("info", "query", result="PASS" if kept else "EMPTY", ms=(time.monotonic() - tq) * 1000,
                    **feed.tags, q=q, cat=cats, limit=limit, items=len(items), kept=kept, dropped=dropped,
                    **({"hash_from": hash_from} if hash_from else {}),
                    **{k: counts[k] for k in ("no_infohash", "no_title") if counts[k]},
                    **({"kept_unnumbered": unnumbered} if unnumbered else {}),
                    **({"over_cap": over_cap, "cap": MAX_ITEMS} if over_cap else {}),
                    **({"titles_cut": len(cut), "longest": max(cut), "title_max": TITLE_MAX} if cut else {}),
                    **job.about)
            if titles:
                # The only evidence of what the title check did on a free-text search (as a
                # free-text add-on logs it): which titles were dropped, and why. A `no_infohash` item
                # is listed too, with the scheme of its link, enclosure and guid (never their values).
                job.say("debug", "dropped", **feed.tags, q=q, titles=titles[:DROPPED_TITLES_LOGGED],
                        more=max(0, len(titles) - DROPPED_TITLES_LOGGED))
    except FeedError as exc:
        res.error = exc
        if exc.kind in _PAUSE_KINDS and exc.retry_after and not _backoff_until(job, feed):
            _remember(job, "backoff", feed, {"until": time.time() + min(exc.retry_after, BACKOFF_MAX_S),
                                             "cause": skip_reason(exc)})
    return _finish(job, feed, res, t0, queries)


def _suggest(reason: str, cause: str, kind: str) -> str:
    """A failed or skipped feed's `suggest`: the reason's own, a `backoff` line's followed by its cause's advice
    (the pointer to Prowlarr stays on every line of a pause), else the kind's."""
    if reason == "backoff":
        return f"{_SUGGEST['backoff']}; {_SUGGEST.get(cause, _SUGGEST['rate_limited'])}"
    return (_SUGGEST.get(reason) or
            _SUGGEST.get(kind, "this feed is skipped for this call; the others still answer"))


def _finish(job: Job, feed: Feed, res: FeedResult, t0: float, queries: list[str]) -> FeedResult:
    """The feed's one `feed` line (unless the call is over), from `res`."""
    ms = (time.monotonic() - t0) * 1000
    with job.lock:
        if job.closed:
            return res
        counts = dict(res.counts)
        if res.error is not None:
            skip = res.error.kind in _SKIP_KINDS
            reason = skip_reason(res.error) if skip else ""
            cause = res.error.cause if reason == "backoff" else ""
            stored = res.error.stored_cause if cause else ""        # the record's own name, when renamed
            (job.log.info if skip else job.log.warn)(
                COMPONENT, "feed", result="SKIP" if skip else "PARTIAL" if res.releases else "FAIL", ms=ms,
                **feed.tags, kind=res.error.kind, **({"reason": reason} if skip else {}),
                **({"cause": cause} if cause else {}), **({"stored_cause": stored} if stored else {}),
                error=str(res.error), **counts, **job.about,
                suggest=_suggest(reason, cause, res.error.kind))
        elif job.fresh:                                 # a check reads caps only: say so, not "an empty search"
            job.log.info(COMPONENT, "feed", result="PASS", ms=ms, **feed.tags, checked="caps", **job.about)
        else:
            job.log.info(COMPONENT, "feed", result="PASS" if res.releases else "EMPTY", ms=ms, **feed.tags,
                         queries=len(queries) - len(res.skipped), **counts, **job.about)
        res.logged = True
    return res


# ----------------------------------------------------------------------------- the add-on call

def _margin(budget: float) -> float:
    """What the add-on keeps back from its budget, so it answers before the registry gives up on it."""
    return min(max(1.0, budget * 0.1), 5.0)


def run(ctx, sources: list[Source], queries: list[str], cats: list[int], keep, about: dict, fresh: bool = False,
        budget_s: float | None = None) -> list[FeedResult]:
    """Every server, then every feed, side by side (at most `MAX_FEED_WORKERS` at once) on daemon threads, until
    all are done or the add-on's own deadline: its budget (`indexerTimeoutSeconds`) minus a margin, so the
    add-on answers before the registry gives up on it.

    Each server is one task (`torznab_prowlarr.resolve`): a Torznab feed is searched in it; a Prowlarr's indexer
    feeds join the queue as tasks of their own. Tasks are taken round-robin across servers (every server's
    detection first, then every server's first feed, then every server's second, ...), so one server with many
    slow feeds or a blackholed one costs that server only. At the deadline the call closes (`Job.closed`): a
    worker takes no further task, a task never started is logged `SKIP not_started`, one still running is logged
    once (`timeout`, `PARTIAL` with the releases of its finished queries) and writes nothing after. Results come
    back in the servers file's order (a Prowlarr's indexers in its own order), whatever order they finished in.

    `fresh` is the Accounts check (`check`): every server detected and every list and caps read again, whatever
    the cache holds, under `budget_s` (default `indexerTimeoutSeconds`)."""
    budget = float(budget_s) if budget_s else indexers.budget(ctx.settings)
    margin = _margin(budget)
    log = ctx.log
    job = Job(ctx, time.monotonic() + max(0.5, budget - margin), cats, about)
    job.fresh = fresh
    log.info(COMPONENT, "plan", result="PASS", servers=[s.label for s in sources], queries=queries,
             cat=cats, budget_s=budget, deadline_s=round(budget - margin, 1), workers=MAX_FEED_WORKERS, **about)
    seq = itertools.count()
    # `order` (server, feed) is the output order; the queue's key (feed, server) is the order tasks are taken in.
    tasks: dict[tuple, tuple] = {}                     # order -> ("server", Source) | ("feed", Feed)
    todo: list = []
    for i, s in enumerate(sources):
        tasks[(i, -1)] = ("server", s)
        heapq.heappush(todo, ((-1, i), next(seq), (i, -1)))
    claimed = {s.feed.url for s in sources}
    started: dict[tuple, FeedResult] = {}
    done: set[tuple] = set()
    replaced: set[tuple] = set()                       # a Prowlarr's server task, replaced by its indexer feeds
    state = {"expanding": 0}
    cv = threading.Condition(job.lock)

    def worker():
        while True:
            with cv:
                while not job.closed and job.left() > 0 and not todo and state["expanding"]:
                    cv.wait(max(0.01, job.left()))
                if job.closed or job.left() <= 0 or not todo:
                    return
                _, _, order = heapq.heappop(todo)
                what, obj = tasks[order]
                res = started[order] = new_result(obj.feed if what == "server" else obj)
                state["expanding"] += what == "server"
            children: list[Feed] = []
            try:
                if what == "server":
                    children = torznab_prowlarr.resolve(job, obj, queries, keep, res)
                else:
                    run_feed(job, obj, queries, keep, res)
            except Exception as exc:                    # noqa: BLE001 - one feed's bug must not cost the others
                res.error = FeedError("error", res.feed.mask(f"{type(exc).__name__}: {exc}"))
                job.say("warn", "feed", result="FAIL", **res.feed.tags, kind="internal", error=str(res.error),
                        suggest="an unexpected error in the Torznab add-on; the other feeds still answer")
                res.logged = True
            with cv:
                if what == "server":
                    state["expanding"] -= 1
                    if children and not job.closed:
                        replaced.add(order)
                        for j, f in enumerate(children):
                            if f.url in claimed:
                                log.info(COMPONENT, "feed", result="SKIP", **f.tags, kind="duplicate", **about,
                                         reason="duplicate",
                                         why="another server in the file is this same feed; it is asked once")
                                continue
                            claimed.add(f.url)
                            tasks[(order[0], j)] = ("feed", f)
                            heapq.heappush(todo, ((j, order[0]), next(seq), (order[0], j)))
                done.add(order)
                cv.notify_all()

    for n in range(MAX_FEED_WORKERS):
        threading.Thread(target=worker, name=f"torznab-{n}", daemon=True).start()
    out: list[FeedResult] = []
    with cv:
        while len(done) < len(tasks):
            left = job.left()
            if left <= 0:
                break
            cv.wait(left)
        job.closed = True
        for order in sorted(tasks):
            if order in replaced:
                continue
            what, obj = tasks[order]
            f = obj.feed if what == "server" else obj
            res = started.get(order)
            if res is None:
                log.info(COMPONENT, "feed", result="SKIP", **f.tags, kind="not_started", **about,
                         reason="not_started",
                         why=f"no worker was free before the add-on's {budget - margin:.1f} s deadline",
                         suggest="raise indexerTimeoutSeconds, or list fewer feeds")
                out.append(FeedResult(feed=f, error=FeedError("not_started", "not started before the deadline")))
                continue
            if not res.logged:
                res.error = FeedError("timeout", f"no answer within the add-on's {budget - margin:.1f} s")
                log.warn(COMPONENT, "feed", result="PARTIAL" if res.releases else "FAIL", **res.feed.tags,
                         kind="timeout", error=str(res.error), **dict(res.counts), **about,
                         suggest="the releases of its finished queries are kept; " + _SUGGEST["timeout"]
                         if res.releases else _SUGGEST["timeout"])
                res.logged = True
            out.append(FeedResult(feed=res.feed, releases=list(res.releases), rejected=list(res.rejected),
                                  counts=dict(res.counts), error=res.error, skipped=list(res.skipped)))
    return out


# How the error that ends a call names each feed's failure (`failed`): short, and never the server's own text.
_KIND_WORDS = {"wrong_key": "wrong API key", "login": "the server asks for a login",
               "error": "the server answered an error", "rate_limited": "asked for a pause",
               "indexer_paused": "paused by Prowlarr", "indexer_failed": "failed in Prowlarr",
               "http_status": "an HTTP error", "unreachable": "unreachable",
               "timeout": "no answer in time", "not_started": "not asked in time", "too_large": "answer too large",
               "redirect": "redirects to another host", "unsafe": "an unsafe answer", "not_xml": "not a Torznab answer",
               "not_torznab": "not a Torznab feed", "no_search": "search unavailable",
               "no_category": "no anime category", "not_address": "not a Torznab or Prowlarr address",
               "no_indexer": "no enabled torrent indexer with an anime or movie category",
               "bad_list": "Prowlarr's indexer list could not be read"}


def failed(results: list[FeedResult]) -> core.StageError | None:
    """The error for a call in which no feed answered (every one failed, was skipped or ran out of time), or
    None when at least one did (PASS, EMPTY or PARTIAL). Returned by `episode_search`/`text_search` as a raise,
    so the registry counts the add-on as failed and the play names the failure, never "no releases,
    check that the episode aired". Built from each feed's label and kind only, so it holds no key."""
    if not results or any(r.error is None or r.releases for r in results):
        return None
    # One part per server for a Prowlarr's indexers: "host:9696: 30 indexers unreachable".
    groups: dict[str, list] = {}
    for r in results:
        groups.setdefault(r.feed.server if r.feed.indexer_id else "\0" + r.feed.label, []).append(r)
    parts = []
    for key, rs in groups.items():
        words = [_KIND_WORDS.get(r.error.kind, r.error.kind) for r in rs]
        if len(rs) == 1:
            parts.append(f"{rs[0].feed.label}: {words[0]}")
            continue
        counts = {w: words.count(w) for w in dict.fromkeys(words)}
        parts.append(f"{key}: {len(rs)} indexers {words[0]}" if len(counts) == 1 else
                     f"{key}: {len(rs)} indexers: " + ", ".join(f"{n} {w}" for w, n in counts.items()))
    kinds = {r.error.kind for r in results}
    suggest = (_SUGGEST.get(next(iter(kinds)), "the log's indexers.torznab feed line says why") if len(kinds) == 1
               else "the log's indexers.torznab feed lines say why each feed failed")
    if kinds == {"indexer_paused"}:             # the viewer reads this one, not the log's guess at a cause
        suggest = say.paused_where(len(results))
    elif kinds == {"indexer_failed"}:           # Prowlarr's own log, in the viewer's words
        suggest = say.FAILED_WHERE
    return core.StageError(COMPONENT, "Torznab: no feed answered (" + "; ".join(parts) + ")", suggest,
                           kinds=sorted(kinds), feeds=len(results))


def merged(results: list[FeedResult]) -> list[dict]:
    """The feeds' releases as one list, one per infohash, the earlier feed's record winning."""
    out: dict[str, dict] = {}
    for res in results:
        for rel in res.releases:
            out.setdefault(rel["info_hash"], rel)
    return list(out.values())


def categories(movie: bool) -> list[int]:
    return [CAT_ANIME, CAT_MOVIE] if movie else [CAT_ANIME]


def episode_search(ctx, query, sources: list[Source]) -> list[dict]:
    """The add-on's `episode_search`: the plan (`episode_plan`), every feed, the plugin's own title check
    (`query.matches`, as for a words-based add-on). A movie's unnumbered release, and on the pack pass a
    pack, is kept when only the episode test fails: the prefilter checks a pack's range."""
    movie = bool(getattr(query, "movie", False))
    own_pairs = frozenset(getattr(query, "movie_pairs", None) or ()) if movie else frozenset()
    single = bool(getattr(query, "single_episode", False)) and not movie
    about = {"episode": query.episode, "pack_pass": query.pack_pass, "movie": movie,
             **({"movie_pairs": [list(p) for p in sorted(own_pairs)]} if movie else {}),
             **({"single_episode": True} if single else {})}
    if query.pack_pass and movie:
        ctx.log.info(COMPONENT, "plan", result="SKIP", reason="movie_no_packs",
                     why="a movie has no packs; its titles were asked already", **about)
        return []
    queries, capped = episode_plan(query, movie)
    if capped:
        ctx.log.info(COMPONENT, "plan", result="CAPPED", sent=queries, skipped=capped,
                     cap=MAX_PACK_QUERIES if query.pack_pass else MAX_EPISODE_QUERIES,
                     reason="at most this many queries per feed, highest value first", **about)
    if not queries:
        ctx.log.warn(COMPONENT, "plan", result="EMPTY", reason="no Latin-script title to search by", **about)
        return []

    readings: list = []                                 # [name, kept/dropped, why] of a one-episode entry
    packs: list = []                                    # [pack_by, name] of each pack the pack pass kept
    franchise_drops: list = []                          # [name, why] of each single named as another show
    franchise_kept: list = []                           # [name, words, why] of each kept near-match
    ova_drops: list = []                                # [name, why] of each OVA a TV series dropped

    def pack_row(pack_by: str, title: str) -> list:
        """`[pack_by, name]` of a kept pack, and, when the own-season words rule read it (it states the entry's own
        season, for a later season), why it stayed and the words it read (`query.own_season_reading`)."""
        own = getattr(query, "own_season_reading", None)
        reading = own(title) if own else None
        return [pack_by, title, *([reading.why, list(reading.words)] if reading is not None else [])]

    def keep(rel):
        """(keep, code, why). `query.matches` first; a movie's release and a pack on the pack pass that fail
        it on the episode alone are asked again without the episode test (the same show and season rule,
        `score.release_matches_target(check_episode=False)`), so another show is never kept.
        For a movie, a kept name that states an episode other than its own ani.zip pair is dropped
        (`movie_episode`): `query.matches` checks it after its own keep, and the fallback here after its keep. The
        movie's own release names none; the name that does is another show's episode carrying the movie's words.
        Counted and listed in the `dropped` line, not the picker; a name of another show still reads `series`.
        A one-episode entry's names are read by `query.matches` (`single_episode`); each reading is kept
        for the `single_episode` line, and a name kept without the episode test counts as unnumbered."""
        pack_by = query.pack_by(rel["title"]) if query.pack_pass else ""
        if query.pack_pass and not pack_by:
            refused = query.pack_refused(rel["title"])
            return False, "pack", f"{PACK_ONLY} ({refused})" if refused else PACK_ONLY
        ok, why = query.matches(rel["title"])
        code = (why or "").split()[0] if why else ""
        if single and not query.pack_pass:
            readings.append([rel["title"], "kept" if ok else "dropped", why])
        if code == score.FRANCHISE_SHOW and why.endswith(f"({score.OVA_ENTRY_TAG})"):
            ova_drops.append([rel["title"], why])       # not a franchise show's drop, counted apart
        elif code == score.FRANCHISE_SHOW:
            franchise_drops.append([rel["title"], why])
        elif ok and not pack_by and getattr(query, "franchise_shows", ()):
            reading = query.franchise_reading(rel["title"])
            if reading and reading.near:
                franchise_kept.append([rel["title"], sorted(reading.words), reading.why])
        if ok:
            if pack_by:
                packs.append(pack_row(pack_by, rel["title"]))
                why = f"pack ({pack_by})"           # every kept pack says what made it one
            return True, "unnumbered" if code == "single_episode" else "ok", why
        if code == "episode" and (movie or query.pack_pass):
            ok, why = score.release_matches_target(rel["title"], query.variants, query.episode, query.alt_numbers,
                                                   target_season=query.season, check_episode=False,
                                                   titles=getattr(query, "titles", ()))
            if ok and movie:
                reason = score.movie_episode_reason(rel["title"], own_pairs)
                if reason:
                    return False, "movie_episode", reason
            # The franchise reading was once asked here of a `Part 3 - 01` single read as a pack.
            # It is no pack now, and the pack pass leaves it above. A season pack states the entry's own season and
            # only parts its titles state, so the franchise reading's exact title and part never drop it; the
            # own-season words rule still does, when it adds a word before its season statement (`Show Driftwood 2nd
            # Season [Batch]`) or, on a TV series, another year.
            own_season = getattr(query, "own_season_reason", None)
            reason = own_season(rel["title"]) if ok and query.pack_pass and own_season else ""
            if reason:
                return False, score.FRANCHISE_SHOW, reason
            if ok:
                if pack_by:
                    packs.append(pack_row(pack_by, rel["title"]))
                return True, "unnumbered", "movie" if movie else f"pack ({pack_by})"
            return False, why.split()[0], why
        return False, code, why

    results = run(ctx, sources, queries, categories(movie), keep, about)
    if query.pack_pass:
        # Which pattern made each kept title a pack, in one line after the feeds.
        seen = list({p[1]: p for p in list(packs)}.values())
        by: dict[str, int] = {}
        for pack_by, *_ in seen:
            kind = pack_by.split(":")[0]
            by[kind] = by.get(kind, 0) + 1
        ctx.log.info(COMPONENT, "packs", result="PASS" if seen else "EMPTY", episode=query.episode,
                     season=query.season, kept=len(seen), by=by, names=seen[:PACK_NAMES_LOGGED],
                     more=max(0, len(seen) - PACK_NAMES_LOGGED),
                     reason="a title is a pack when it says batch or names a range, or when it states the "
                            "entry's own season and no episode; season packs are read only when the entry's titles "
                            "state a season; a pack the own-season words rule read carries why it stayed and the words "
                            "it read before its season statement")
    if single and not query.pack_pass:
        # Why each name of a one-episode entry was kept or dropped, in one line after the feeds.
        seen = list({r[0]: r for r in list(readings)}.values())
        kept = [r for r in seen if r[1] == "kept"]
        ctx.log.info(COMPONENT, "single_episode", result="PASS" if kept else "EMPTY", episode=query.episode,
                     kept=len(kept), dropped=len(seen) - len(kept), names=seen[:SINGLE_EPISODE_NAMES_LOGGED],
                     # The related-show drops, counted past the names cap too.
                     dropped_related=sum(1 for r in seen if str(r[2] or "").startswith("single_episode names related")),
                     more=max(0, len(seen) - SINGLE_EPISODE_NAMES_LOGGED),
                     related_shows=sorted({r[0] for r in getattr(query, "related_shows", ()) or ()}),
                     # The one-episode prequels, and the prequel keeps past the names cap.
                     prequel_shows=sorted({r[0] for r in getattr(query, "prequel_shows", ()) or ()}),
                     kept_prequel=sum(1 for r in kept if str(r[2] or "").endswith(f"({score.PREQUEL_TAG})")),
                     reason=score.SINGLE_EPISODE_REASON)
    franchise = getattr(query, "franchise_shows", ()) or ()
    if franchise and not query.pack_pass:
        # Which singles a later-season entry dropped as another show's, and the show each named.
        seen = list({r[0]: r for r in list(franchise_drops)}.values())
        near = list({r[0]: r for r in list(franchise_kept)}.values())
        ctx.log.info(COMPONENT, "franchise_show", result="PASS" if seen else "EMPTY", episode=query.episode,
                     dropped=len(seen), names=seen[:FRANCHISE_SHOW_NAMES_LOGGED],
                     # The OVA drops share the code, not this line's reason; each is
                     # named on the `dropped` line and `index.search ova_entry`.
                     ova_entry=len({r[0] for r in list(ova_drops)}),
                     more=max(0, len(seen) - FRANCHISE_SHOW_NAMES_LOGGED), shows=len({r[0] for r in franchise}),
                     # The kept singles whose words share one with a franchise title,
                     # with the words read and why each stayed, so "why wasn't X dropped?" is in the log.
                     kept_near=near[:FRANCHISE_SHOW_NAMES_LOGGED],
                     kept_near_more=max(0, len(near) - FRANCHISE_SHOW_NAMES_LOGGED),
                     reason="a later season: a single whose words before its episode marker are exactly one of the "
                            "franchise's other shows' titles is that show's episode")
    error = failed(results)
    if error is not None:
        raise error
    for res in results:                                 # on the add-on's own thread, finished feeds only
        for rel, code, why in res.rejected:
            query.reject(rel, code, why)
    return merged(results)


def text_search(ctx, query, sources: list[Source]) -> list[dict]:
    """The add-on's `text_search`: the viewer's words, as typed, once per feed; nothing filtered."""
    words = str(query.words or "").strip()
    movie = bool(getattr(query, "movie", False))
    if not words:
        return []
    results = run(ctx, sources, [words], categories(movie), lambda rel: (True, "ok", ""), {"movie": movie})
    error = failed(results)
    if error is not None:
        raise error
    return merged(results)


def addon(sources: list[Source]) -> ModuleType:
    """The add-on as the registry holds it: a module object with `API_VERSION`, `NAME` and the two functions,
    bound to these feeds. It defines no other capability (`Indexer.offers` looks for callables by name)."""
    module = ModuleType("omarchy_anime_builtin_torznab")
    module.API_VERSION = 1
    module.NAME = NAME
    module.episode_search = lambda ctx, query: episode_search(ctx, query, sources)
    module.text_search = lambda ctx, query: text_search(ctx, query, sources)
    return module


def _transient(error: FeedError) -> bool:
    """A failure worth trying again soon rather than fixing (for the Accounts sheet): no answer, a timeout, a rate
    limit, an indexer Prowlarr has paused (its pause ends by itself), a 5xx (a Prowlarr indexer's 500,
    `indexer_failed`, included, as it was while it read as `http_status`)."""
    return (error.kind in ("unreachable", "timeout", "rate_limited", "indexer_paused", "indexer_failed", "not_started")
            or (error.kind == "http_status" and (error.status or 500) >= 500))


def check(http, log, server: torznab_servers.Server, settings: dict | None = None,
          budget_s: float | None = None) -> dict:
    """The Accounts "check": through the same pool and deadline as a play (`run(fresh=True)`), the server is detected
    afresh, a Prowlarr's indexer list read again and every kept feed's caps read again, whatever the cache holds;
    what was found is remembered for the plays that follow (a failure never erases a remembered list, and never
    turns a good record into a refusal except for a wrong key or a login). Nothing is searched.

    Runs on the caller's thread for up to `budget_s` (default `indexerTimeoutSeconds`): the Accounts sheet runs it off
    the UI thread and passes a short budget. Returns `{ok, server, kind, problem, message, transient, feeds, dropped}`:
    - `ok`: the server answers and at least one of its feeds can be searched;
    - `kind`: `prowlarr` or `torznab` as this check found it (None when it could not tell);
    - `problem`/`message`: the server's failure (or, when no feed can be searched, the most common feed
      failure) and its words ("wrong API key", "the server asks for a login", "unreachable", "not a
      Torznab or Prowlarr address", ...); `transient` when trying again soon may help (no answer, a timeout,
      a rate limit, a 5xx);
    - `feeds`: one `{label, indexer, indexer_id, ok, problem, message}` per feed it will search;
    - `dropped`: a Prowlarr's indexers left out, by reason (`over_cap` included).
    Holds no key. A Prowlarr indexer's name is the server's own text (scrubbed at every sink, drawn as plain text);
    nothing else the server said is in it."""
    settings = {**core.DEFAULT_SETTINGS, **(settings or {})}
    src, why = source_of(server)
    if src is None:
        return {"ok": False, "server": "", "kind": None, "problem": "invalid", "message": why, "transient": False,
                "feeds": [], "dropped": {}}
    t0 = time.monotonic()
    started = time.time()
    ctx = indexers.Context(http=http, log=log, settings=MappingProxyType(settings), addon=STEM)
    results = run(ctx, [src], [], [CAT_ANIME, CAT_MOVIE], lambda rel: (True, "ok", ""), {"check": True},
                  fresh=True, budget_s=budget_s)
    rec = torznab_prowlarr.remembered(ctx, src) or {}
    kind = rec.get("kind") if float(rec.get("ts") or 0) >= started and rec.get("kind") != "none" else None
    server_failed = [r for r in results if not r.feed.indexer_id and r.error is not None and kind != "torznab"]
    if any(r.feed.indexer_id for r in results):
        kind = "prowlarr"
    feeds = [{"label": r.feed.label, "indexer": r.feed.indexer, "indexer_id": r.feed.indexer_id,
              "ok": r.error is None, "problem": r.error.kind if r.error else "",
              "message": _KIND_WORDS.get(r.error.kind, r.error.kind) if r.error else ""}
             for r in results if not any(r is f for f in server_failed)]
    error = None
    if server_failed:
        error = server_failed[0].error
    elif not any(f["ok"] for f in feeds):
        failures = [r.error for r in results if r.error is not None]
        # A tie goes to the first kind by name, not to the set's hash order
        common = (max(sorted({e.kind for e in failures}), key=lambda k: sum(e.kind == k for e in failures))
                  if failures else "")
        error = next((e for e in failures if e.kind == common), None)
    problem = error.kind if error is not None else ""
    out = {"ok": not problem, "server": src.label, "kind": kind, "problem": problem,
           "message": _KIND_WORDS.get(problem, problem) if problem else "",
           "transient": bool(problem) and _transient(error), "feeds": feeds,
           "dropped": dict(rec.get("dropped") or {}) if kind == "prowlarr" else {}}
    log.info(COMPONENT, "check", result="PASS" if out["ok"] else "FAIL", ms=(time.monotonic() - t0) * 1000,
             server=src.label, kind=kind, problem=problem or None, feeds=len(feeds),
             failing=sum(not f["ok"] for f in feeds), dropped=out["dropped"],
             suggest="" if out["ok"] else _SUGGEST.get(problem, "the log's indexers.torznab lines say why"))
    return out


def suggest(problem: str) -> str:
    """What to do about a feed's or server's failure kind, for a log line's `suggest`."""
    return _SUGGEST.get(str(problem or ""), "the log's indexers.torznab lines say why")


def _remembered_paths(src: Source, keep_urls=frozenset()) -> list:
    """Every cache file the add-on keeps about one server: its detection record (with a Prowlarr's list), and
    the caps and back-off of its own feed and of each indexer feed remembered for it, except a feed in `keep_urls`
    (another server searches it too)."""
    root = cache_root()
    feeds, _, _ = torznab_prowlarr.known_feeds(src)
    paths = [root / "servers" / f"{src.record_key}.json"]
    for feed in [src.feed] + list(feeds):
        if feed.url not in keep_urls:
            paths += [root / "caps" / f"{feed.cache_key}.json", root / "backoff" / f"{feed.cache_key}.json"]
    return list(dict.fromkeys(paths))


def feed_urls(server: torznab_servers.Server) -> set:
    """The feed URLs a server is searched through now (its own and a remembered Prowlarr's), without the network."""
    src, _ = source_of(server)
    if src is None:
        return set()
    return {src.feed.url} | {f.url for f in torznab_prowlarr.known_feeds(src)[0]}


def forget(server: torznab_servers.Server, keep: list | None = None) -> tuple[list, list]:
    """Delete what the add-on remembers about a server (it was removed, or an add of it was not saved), keeping
    any feed one of `keep` still searches. Returns (paths deleted, [(path, error)] that could not be). Local only."""
    src, _ = source_of(server)
    if src is None:
        return [], []
    keep_urls = set().union(*(feed_urls(k) for k in keep or [])) if keep else set()
    removed, failed = [], []
    for path in _remembered_paths(src, keep_urls):
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            continue
        except OSError as exc:
            failed.append((str(path), f"{type(exc).__name__}: {exc}"))
    return removed, failed


def snapshot(server: torznab_servers.Server) -> dict:
    """{path: bytes or None} of what the add-on remembers about a server, before a check that may overwrite it
    (a failed key change for a listed server must leave its remembered detection as it was)."""
    src, _ = source_of(server)
    if src is None:
        return {}
    out = {}
    for path in _remembered_paths(src):
        try:
            out[path] = path.read_bytes()
        except FileNotFoundError:
            out[path] = None
        except OSError:
            continue
    return out


def restore(snap: dict) -> list:
    """Put back what `snapshot` saw: each file as it was, and a file that was not there removed. Returns
    [(path, error)] for any that could not be restored."""
    failed = []
    for path, body in snap.items():
        try:
            if body is None:
                path.unlink(missing_ok=True)
            else:
                core.write_bytes_atomic(path, body)
        except OSError as exc:
            failed.append((str(path), f"{type(exc).__name__}: {exc}"))
    return failed


from . import torznab_prowlarr  # noqa: E402 - it imports this module; nothing of it is used at import time
