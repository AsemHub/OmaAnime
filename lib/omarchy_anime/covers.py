"""Cover-image cache. The CLI downloads AniList covers; the shell only ever reads local files.

Why this exists: quickshell segfaulted once inside `QQuickPixmapReader`, in Qt's one-time
TLS initialisation for the **process's first https request** — which, for this plugin, is an AniList
cover. The crash is below Qt, in libcrypto, and is not ours to fix; the exposure is ours to remove.
First-party Omarchy plugins never do https from QML: they shell out and read local files. After this
module exists, so do we — `coverPath` (a filesystem path) sits next to `coverImage` (the URL) in
every command that returns media, and the QML reads only the path.

Facts this module is built on, read from the source on 2026-09-15:
  - an AniList cover URL is immutable: the file name carries a content token
    (`bx400002-uXeV9auLT2sQ.jpg`) and the CDN answers `cache-control: max-age=2678400`. A cached
    file can therefore never be stale, only unused — which is why `prune` deletes by last *access*.
  - a `medium` cover is ~9.5 KB, a `large` one ~41 KB (measured with `curl -w`), so the 512 KiB cap
    below is ~12x the largest real cover.
  - 10 covers at concurrency 4 took 0.41 s, so a 100-cover warm costs about 4 s and 1 MB.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

from . import core
from .core import CACHE_DIR, Http, Log, StageError

COVERS_DIR = CACHE_DIR / "covers"
STAMP_NAME = ".pruned"

# The file name must never come from the network: the extension comes from the URL and is allowlisted; the body must sniff as an allowlisted image (see sniff_type).
EXT_FOR_TYPE = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/jpg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
}
ALLOWED_EXT = {ext for exts in EXT_FOR_TYPE.values() for ext in exts}

# The body's own magic bytes. TVDB serves clear logos as PNG bytes under a `.jpg` URL (measured
# 2026-09-21), so the Content-Type/extension check below accepts a body whose *sniffed* type is
# allowlisted even when the name disagrees. The file keeps its URL-derived name: `have(url)` and
# `cover_path(url)` derive the name from the URL, so a rename would never be found again.
def sniff_type(body: bytes) -> str:
    head = bytes(body[:12]) if body else b""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return ""

MAX_BYTES = 512 * 1024              # per cover; a larger response is discarded, not truncated
DEFAULT_WORKERS = 4
DEFAULT_BUDGET_S = 8.0
PRUNE_MAX_AGE_DAYS = 30
PRUNE_MAX_BYTES = 64 * 1024 * 1024
PRUNE_EVERY_S = 86400

SIZES = ("large", "medium")


# ----------------------------------------------------------------------------- paths

def _cache_name(url: str) -> str:
    """<sha1 of the url><ext>, or "" when the URL's extension is not an allowlisted image.

    Deliberately not `urlparse` + `os.path.splitext`: `attach()` runs this twice per media on every
    list command, and the two calls measured 53 us each (10.6 ms for one top-100 payload). String
    slicing plus one sha1 is about 6x cheaper and the result is identical.
    """
    if not url or not isinstance(url, str):
        return ""
    cut = len(url)
    for mark in ("?", "#"):
        i = url.find(mark)
        if 0 <= i < cut:
            cut = i
    path = url[:cut]
    dot = path.rfind(".")
    if dot <= path.rfind("/"):
        return ""
    ext = path[dot:].lower()
    if ext not in ALLOWED_EXT:
        return ""
    return hashlib.sha1(url.encode("utf-8")).hexdigest() + ext


def cover_path(url: str) -> Path | None:
    """CACHE_DIR/covers/<sha1 of the url>.<ext>, or None when the URL's extension is not an image."""
    name = _cache_name(url)
    return COVERS_DIR / name if name else None


def have(url: str) -> str:
    """The local path when the cover is on disk and non-empty, else "" ("not downloaded yet").

    Called once per cover on every list command, so it stays on os.path/os.stat rather than pathlib.
    """
    name = _cache_name(url)
    if not name:
        return ""
    full = os.path.join(str(COVERS_DIR), name)
    try:
        return full if os.stat(full).st_size > 0 else ""
    except OSError:
        return ""


# ----------------------------------------------------------------------------- download

def fetch_one(http: Http, log: Log, url: str, max_bytes: int = MAX_BYTES) -> str:
    """Download one cover into the cache. Returns its path, or "" and one FAIL line on refusal."""
    p = cover_path(url)
    if p is None:
        log.warn("covers.fetch", "refuse", result="FAIL", url=url,
                 suggest="not an allowlisted image extension (.jpg .jpeg .png .webp)")
        return ""
    cached = have(url)
    if cached:
        return cached
    # https on a public host only, checked here so a refused address costs no request or retry; `Http.request`
    # holds every redirect to the same rule (`public_only`). The cap is applied while reading.
    why = core.public_https_refusal(url)
    if why:
        log.warn("covers.fetch", "refuse", result="FAIL", url=url, error=why,
                 suggest="a cover is fetched only from an https address on a public host")
        return ""
    t0 = time.monotonic()
    try:
        status, body, headers = http.request("covers", "GET", url, expect_json=False, retries=2, max_bytes=max_bytes,
                                             public_only=True)
    except StageError as exc:
        if exc.data.get("reason") == "too_large":
            log.warn("covers.fetch", "too-big", result="FAIL", url=url, cap=max_bytes,
                     suggest="larger than the cap for this asset class; stopped at the cap, nothing kept")
            return ""
        log.warn("covers.fetch", "error", result="FAIL", url=url, error=str(exc))
        return ""
    ms = (time.monotonic() - t0) * 1000
    if status != 200 or not isinstance(body, (bytes, bytearray)) or not body:
        log.warn("covers.fetch", "status", result="FAIL", url=url, status=status, ms=ms)
        return ""
    ctype = str((headers or {}).get("Content-Type") or (headers or {}).get("content-type") or "")
    ctype = ctype.split(";")[0].strip().lower()
    sniffed = sniff_type(body)
    if not sniffed:
        log.warn("covers.fetch", "content-type", result="FAIL", url=url, status=status,
                 content_type=ctype, ext=p.suffix, suggest="the body is not an image; discarded")
        return ""
    if p.suffix not in EXT_FOR_TYPE.get(ctype, set()) or p.suffix not in EXT_FOR_TYPE.get(sniffed, set()):
        # Not a refusal: the bytes are a real image of an allowlisted type, only the name disagrees.
        log.info("covers.fetch", "mismatch", result="PASS", url=url, content_type=ctype, sniffed=sniffed,
                 ext=p.suffix)
    if len(body) > max_bytes:
        log.warn("covers.fetch", "too-big", result="FAIL", url=url, bytes=len(body), cap=max_bytes,
                 suggest="larger than the cap for this asset class; discarded, not truncated")
        return ""
    try:
        core.write_bytes_atomic(p, body)    # atomic: a half-written file is never handed to QML
    except OSError as exc:
        log.warn("covers.fetch", "write", result="FAIL", url=url, error=str(exc))
        return ""
    log.debug("covers.fetch", "done", result="PASS", url=url, status=status, bytes=len(body), ms=ms)
    return str(p)


def fetch_many(http: Http, log: Log, urls, *, workers: int = DEFAULT_WORKERS,
               budget_s: float = DEFAULT_BUDGET_S, max_bytes: int = MAX_BYTES) -> dict[str, str]:
    """Download every missing cover, at most `workers` at a time, for at most `budget_s` seconds.

    Returns {url: path} for every cover that is now on disk. Whatever the budget cut off is simply
    left for the next warm — covers are never required for a frame to render.

    It returns at the budget. It used a `with ThreadPoolExecutor`, whose exit waits for the downloads
    already running (`cancel()` cannot stop one), and `concurrent.futures` also joins its workers when the process
    exits: a stalled image host held the play's now-playing step about 32 s past its 3 s budget. The workers are
    daemon threads now, so neither this call nor the process waits for one. A late download is not in the answer;
    its file, written atomically, is there for the next warm, and a worker cut off by the process exit can leave
    a `.tmp.` file behind, which `prune` ages out with the covers. A worker takes no new URL once the budget is
    spent.
    """
    wanted, seen = [], set()
    for url in urls or []:
        if url and url not in seen:
            seen.add(url)
            wanted.append(url)
    out = {u: have(u) for u in wanted if have(u)}
    missing = [u for u in wanted if u not in out]
    t0 = time.monotonic()
    log.debug("covers.warm", "start", urls=len(wanted), hit=len(out), miss=len(missing))
    if missing:
        import queue
        deadline = t0 + max(0.1, float(budget_s))
        todo: queue.SimpleQueue = queue.SimpleQueue()
        done: queue.SimpleQueue = queue.SimpleQueue()
        for u in missing:
            todo.put(u)

        def work() -> None:
            while time.monotonic() < deadline:
                try:
                    u = todo.get_nowait()
                except queue.Empty:
                    return
                try:
                    done.put((u, fetch_one(http, log, u, max_bytes), None))
                except BaseException as exc:            # noqa: BLE001 - handed to the caller's thread, as before
                    done.put((u, "", exc))

        for _ in range(min(max(1, int(workers)), len(missing))):
            threading.Thread(target=work, name="covers-fetch", daemon=True).start()
        for answered in range(len(missing)):
            try:
                u, path, exc = done.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                log.warn("covers.warm", "budget", result="FAIL", budget_s=budget_s, done=len(out),
                         answered=answered, waiting=len(missing) - answered,
                         suggest="the rest is left for the next warm; downloads still running are not waited for")
                break
            if exc is not None:
                raise exc
            if path:
                out[u] = path
    downloaded = len(out) - (len(wanted) - len(missing))
    written = sum(os.path.getsize(p) for p in out.values() if os.path.exists(p)) if downloaded else 0
    log.info("covers.warm", "done", result="PASS", ms=(time.monotonic() - t0) * 1000, urls=len(wanted),
             hit=len(wanted) - len(missing), miss=len(missing), downloaded=downloaded,
             failed=len(missing) - downloaded, bytes=written)
    return out


# ----------------------------------------------------------------------------- payload plumbing

def _walk(obj, fn, depth: int = 0) -> None:
    """Call fn on every dict in a JSON payload (depth-capped; AniList payloads are shallow)."""
    if depth > 12:
        return
    if isinstance(obj, list):
        for item in obj:
            _walk(item, fn, depth + 1)
    elif isinstance(obj, dict):
        fn(obj)
        for value in list(obj.values()):
            if isinstance(value, (list, dict)):
                _walk(value, fn, depth + 1)


def attach(obj, sizes=SIZES):
    """Add `coverPath` next to every `coverImage` in a payload, computed from the cache directory.

    Read-only: nothing is downloaded here, so a list command stays a local read. `coverImage` stays
    where it is, so a future consumer of the URL still has it — the QML just never looks at it.
    The paths are computed on every call rather than stored in the JSON caches, so a later warm
    makes every subsequent read correct without rewriting or invalidating a single cache file.
    """
    def add(d: dict) -> None:
        ci = d.get("coverImage")
        if isinstance(ci, dict):
            d["coverPath"] = {k: have(ci.get(k) or "") for k in sizes}

    _walk(obj, add)
    return obj


def collect_urls(obj, sizes=SIZES) -> list[str]:
    """Every cover URL in a payload, de-duplicated, in encounter order."""
    out: list[str] = []
    seen: set[str] = set()

    def add(d: dict) -> None:
        ci = d.get("coverImage")
        if not isinstance(ci, dict):
            return
        for key in sizes:
            url = ci.get(key)
            if url and url not in seen:
                seen.add(url)
                out.append(url)

    _walk(obj, add)
    return out


def media_map(obj) -> dict[str, dict]:
    """{media id: coverPath} for everything in a payload that has both — the shape the overlay
    merges into a search result list, so the QML never has to touch a cover URL."""
    out: dict[str, dict] = {}

    def add(d: dict) -> None:
        if isinstance(d.get("coverPath"), dict) and d.get("id") is not None:
            out[str(d["id"])] = d["coverPath"]

    _walk(obj, add)
    return out


def attach_now_playing(now: dict) -> dict:
    """now-playing.json carries one cover URL (`cover`); give it the same treatment."""
    if isinstance(now, dict) and now.get("cover"):
        now["coverPath"] = {"large": have(now.get("cover") or ""),
                            "medium": have(((now.get("media") or {}).get("coverImage") or {}).get("medium") or "")}
    return now


# ----------------------------------------------------------------------------- housekeeping

def _files() -> list[Path]:
    try:
        return [p for p in COVERS_DIR.iterdir() if p.is_file() and not p.name.startswith(".")]
    except OSError:
        return []


def prune(max_age_days: float = PRUNE_MAX_AGE_DAYS, max_bytes: int = PRUNE_MAX_BYTES) -> dict:
    """Delete covers untouched for `max_age_days` (both mtime and atime), then, if the directory is
    still over `max_bytes`, the least recently used ones until it is not."""
    t0 = time.monotonic()
    removed, freed = 0, 0
    cutoff = time.time() - max_age_days * 86400
    alive: list[tuple[float, int, Path]] = []
    for p in _files():
        try:
            st = p.stat()
        except OSError:
            continue
        last = max(st.st_atime, st.st_mtime)
        if st.st_mtime < cutoff and st.st_atime < cutoff:
            try:
                p.unlink()
                removed += 1
                freed += st.st_size
                continue
            except OSError:
                pass
        alive.append((last, st.st_size, p))
    total = sum(size for _, size, _ in alive)
    if total > max_bytes:
        for last, size, p in sorted(alive, key=lambda t: t[0]):
            if total <= max_bytes:
                break
            try:
                p.unlink()
            except OSError:
                continue
            removed += 1
            freed += size
            total -= size
    return {"removed": removed, "freed": freed, "count": len(_files()), "bytes": total,
            "ms": round((time.monotonic() - t0) * 1000, 1)}


def maybe_prune(log: Log | None = None) -> dict | None:
    """Prune at most once a day, so the user never has to run it. Returns None when it was skipped."""
    stamp = COVERS_DIR / STAMP_NAME
    try:
        if time.time() - stamp.stat().st_mtime < PRUNE_EVERY_S:
            return None
    except OSError:
        pass
    core.touch_private(stamp)
    out = prune()
    if log is not None:
        log.info("covers.prune", "daily", result="PASS", **out)
    return out


def status() -> dict:
    files = _files()
    sizes = []
    for p in files:
        try:
            sizes.append((p.stat().st_mtime, p.stat().st_size))
        except OSError:
            continue
    return {"dir": str(COVERS_DIR), "count": len(sizes), "bytes": sum(s for _, s in sizes),
            "oldest": min((m for m, _ in sizes), default=None),
            "newest": max((m for m, _ in sizes), default=None)}
