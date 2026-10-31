"""Episode thumbnails as raw pixels.

mpv's OSD is ASS, and libass cannot draw an image. The one mechanism that can is `overlay-add`
(mpv man page line 14027), and it takes **raw data** — no header, no decoder. So the JPEG that
ani.zip already gave us has to become a BGRA buffer at exactly the size it will be drawn at, and
that conversion is ffmpeg's job, not ours.

The size is part of the cache key because the file has no header: a 160x90 buffer and a 320x180
buffer are both "the thumbnail for episode 3" and nothing in the bytes distinguishes them.

Fetching happens here, in the CLI. Nothing in the shell process may do https, and
the last time images were loaded there, quickshell segfaulted in Qt's `QQuickPixmapReader`.

Fetching is not this module's job either. `covers.py` already downloads a batch of URLs into
a cache with an extension allowlist, a status check, a byte-type check, a size cap and a
Content-Type-vs-extension check — all the hardening that segfault exists to justify — so `ensure` calls
`covers.fetch_many` instead of opening a second, unhardened https path beside it. A useful side
effect: `covers` caches by *URL*, this module caches by *(media, episode, size)*, so converting
the same source image at a new size never refetches it, only re-runs ffmpeg on the local file.

A media's panel is rebuilt about twice per episode (once at play start, once at
the watched threshold — see `ensure`'s docstring), never once per tick. A `covers.fetch_one`-per-
episode loop, bounded by a 3 s *sequential* budget, would therefore only ever acquire a couple of
thumbnails across an entire season, and the menu would look permanently, confusingly half-built —
worse than drawing no thumbnails at all. `ensure` instead collects every URL the panel still needs
and hands the whole list to `covers.fetch_many` in one call, so its worker pool (4 concurrent
downloads by default) fetches a season's worth of small images well inside one budget window.

Conversion stays sequential (one `ffmpeg` subprocess at a time, never parallelised alongside the
fetch), and it is the larger of the two costs, not the rounding error the original design assumed.
**Re-measured on this branch** (7 runs each, a real 640×360 still from the cover cache, warm page
cache): `convert_forms` **~150 ms median** (140–176) and `make_backdrop` **~137 ms median**
(113–197). Spawn cost dominates and the filter graph is close to free: a single plain
`scale=320:180` to rawvideo measured ~144 ms in the same run, i.e. the same as the three-output,
two-blur graph `convert_forms` actually issues. An earlier ~65 ms figure was measured before the row buffer
doubled to 320×180 and one process emitted three forms; it is superseded here.

What that costs a rebuild: a 3 s budget converts about **20 episodes**, so the 49-cell anchor
window fills over roughly **three** 3 s rebuilds rather than one. The budget is deliberately
unchanged — a panel is rebuilt about twice per episode (see `ensure` below), so three rebuilds is
under two episodes of watching, and the nearest-first order means the cells the user is actually
looking at are the ones that land first. `ensure`'s docstring below has how the shared per-call
deadline bounds fetch and convert together.
"""
from __future__ import annotations

import os
import pathlib
import re
import shlex
import subprocess
import threading
import time

from . import core
from .core import CACHE_DIR, Log
from . import covers

THUMBS_DIR = CACHE_DIR / "thumbs"

# The Lua overlay draws at exactly this size (the buffer has no header). One definition; the Lua
# keeps a copy, and a test pins the two equal. 320x180, twice the earlier size.
THUMB_W = 320
THUMB_H = 180
STILL_W, STILL_H = 640, 360            # the blurred JPEG the cards and the loading screen read
LOGO_W, LOGO_H = 640, 360              # the clear logo as BGRA, letterboxed inside this box
# The loading backdrop's raw size. The Lua reads W×H from the file NAME, so only this
# file changes when a measurement picks a different size.
BACKDROP_W, BACKDROP_H = 960, 540
# Gaussian sigma at 640 px wide, scaled by width for other sizes: the frame's colour and
# composition survive, its content does not. A starting value; three are compared on screenshots and
# the user picks.
BLUR_SIGMA = 24
SHOW_ART_MAX_BYTES = 2 * 1024 * 1024   # fanart measured 201..633 KB; covers' 512 KiB is for covers
FFMPEG_TIMEOUT_S = 20
# The most pixels ffmpeg decodes of one source image (`-max_pixels`, before `-i`). A remote still of
# 16000x16000 fits in a PNG of about 250 KB and took 0.9-1.6 s and 348-499 MiB to decode. The largest recorded
# still is 1920x1080; 4096x4096 is 8 times that and twice a 3840x2160 one. Past it ffmpeg writes nothing and
# `_run_ffmpeg` logs the FAIL.
FFMPEG_MAX_PIXELS = 4096 * 4096


def _sigma(width: int) -> float:
    return round(BLUR_SIGMA * int(width) / 640.0, 2)


# How far either side of the cursor one rebuild reaches. The Browse grid shows
# one screen and the player menu five rows; 49 episodes around the cursor covers both with room
# to scroll, and bounds a rebuild to about one budget regardless of show length. The anchor is the
# episode being played, else `progress + 1` -- never "latest": a rewatch from episode 1 wants
# pictures at episode 1.
FETCH_WINDOW = 24


def fetch_order(cells, anchor: int | None, window: int = FETCH_WINDOW) -> tuple[list[dict], int]:
    """The cells worth fetching this call, nearest to `anchor` first, and how many were left
    outside the window. Pure. Cells with no image or no usable episode number are dropped."""
    usable: list[tuple[int, dict]] = []
    for cell in cells or []:
        if not isinstance(cell, dict) or not cell.get("image"):
            continue
        try:
            usable.append((int(cell.get("episode")), cell))
        except (TypeError, ValueError):
            continue
    if anchor is None:
        return [c for _, c in sorted(usable, key=lambda t: t[0])], 0
    a = int(anchor)
    inside = [(ep, c) for ep, c in usable if abs(ep - a) <= int(window)]
    inside.sort(key=lambda t: (abs(t[0] - a), t[0]))
    return [c for _, c in inside], len(usable) - len(inside)


def path_for(media_id: int, episode: int, w: int, h: int, variant: str = "") -> pathlib.Path:
    tag = f".{variant}" if variant else ""
    return THUMBS_DIR / str(int(media_id)) / f"{int(episode)}@{int(w)}x{int(h)}{tag}.bgra"


def still_blur_path(media_id: int, episode: int) -> pathlib.Path:
    return THUMBS_DIR / str(int(media_id)) / f"{int(episode)}.blur.jpg"


def fanart_blur_path(media_id: int) -> pathlib.Path:
    return THUMBS_DIR / str(int(media_id)) / "fanart.blur.jpg"


def logo_path(media_id: int, w: int, h: int) -> pathlib.Path:
    return THUMBS_DIR / str(int(media_id)) / f"logo@{int(w)}x{int(h)}.bgra"


def backdrop_path(w: int, h: int) -> pathlib.Path:
    return THUMBS_DIR / f"backdrop@{int(w)}x{int(h)}.bgra"


def _bgra_ok(path: pathlib.Path, w: int, h: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size == int(w) * int(h) * 4
    except OSError:
        return False


def _file_ok(path: pathlib.Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def cached(media_id: int, cells: list[dict], w: int, h: int) -> dict[int, str]:
    """Which cells already have a correctly-sized CRISP buffer on disk (the blur buffer and the
    blur JPEG are not reported here). No network, no `http` client -- a `stat()` call needs
    neither. This is what a caller with no client at all (`resume_into`, reached from
    a player restart) attaches instead of `ensure`'s network path, so a restart does not silently
    drop every thumbnail `ensure` already fetched for this show. `attach()` is what a surface with
    a payload to fill actually reads every derived-picture key from.

    Never raises: an unreadable cache entry degrades to "not cached" for that one episode, same as
    every other stat-only check in this module.

    `ensure`'s own "already have it" seed no longer delegates to this function -- it
    checks `forms()` instead, and requires all three derived files (crisp, blur buffer, blur
    still), not just the crisp one this function reports. The two intentionally differ: this one
    exists for a caller with no client to fall back to *something* rather than nothing, so a
    client-less restart still shows the crisp buffer `ensure` already made even if the blur forms
    from a later `ensure` call are still pending.
    """
    out: dict[int, str] = {}
    for cell in cells or []:
        try:
            episode = int(cell.get("episode"))
        except (TypeError, ValueError):
            continue
        dest = path_for(media_id, episode, w, h)
        try:
            if dest.is_file() and dest.stat().st_size == int(w) * int(h) * 4:
                out[episode] = str(dest)
        except OSError:
            continue
    return out


def _part(final: pathlib.Path) -> pathlib.Path:
    """The temp name one writer uses for `final`, scoped to this process and thread.

    A literal `<name>.tmp`/`.part` shared by every writer is only safe while one
    process at a time writes a given file, and `Browse.qml`'s `loadDetail()` breaks that by design
    -- it starts `media <id>` and `episodes <id> --write --fetch` together for the same show, and a
    play's `loading_art` can be converting the same episode's forms beside them. Two writers on one
    `<name>.part` means the loser renames a file the winner already moved away (FileNotFoundError)
    and the failure path then unlinks the winner's part out from under it. The pid+thread suffix
    makes the name private, so concurrent writers only ever race on the atomic final rename.

    Both the ffmpeg argv and `_run_ffmpeg`'s rename must derive the name from HERE: the two used to
    build it independently, and a change to one alone would look for a file the other never wrote.
    """
    return final.with_name(f"{final.name}.part.{os.getpid()}.{threading.get_ident():x}")


def _run_ffmpeg(src: pathlib.Path, args: list[str], outputs: list[tuple[pathlib.Path, int | None]],
                timeout_s: float = FFMPEG_TIMEOUT_S, log: Log | None = None) -> bool:
    """Run one ffmpeg command whose outputs are `.part` files; on success rename every output into
    place, on any failure delete every `.part`. `outputs` is (final_path, expected_size or None
    for "just non-empty"). Never raises.

    `timeout_s` is how long the subprocess itself may take, so a caller working to a deadline can
    hand its own remaining time down instead of silently inheriting the 20 s ceiling: a budget that
    only gates *entry* to a step does not bound a step already running. Floored at 0.2 s — a caller
    whose budget is spent should get one honest attempt at a conversion that costs ~150 ms, not an
    instant `TimeoutExpired` (degrade, do not cliff-edge).

    `log` is diagnosis only, never a dependency: with one, a failure says which ffmpeg exit code or
    which size mismatch it was, what ffmpeg itself said, and the exact argv to re-run by hand
    (the caller's own FAIL line knows the episode, not the reason). Without one every
    conversion behaves exactly as before and still just returns False.

    ffmpeg reads `src` on its stdin (`-i pipe:0`; it tells a JPEG, PNG or WebP from its bytes) and runs in the
    outputs' folder, where `args` names each output by its `_part` name alone: neither the picture's cached name (the
    hash of its address) nor the show's folder (its AniList id) is on ffmpeg's command line, which every account on the
    computer can read; its working folder (`/proc/<pid>/cwd`) only its owner can."""
    parts = [(final, _part(final), size) for final, size in outputs]
    cwd = outputs[0][0].parent
    argv = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-max_pixels", str(FFMPEG_MAX_PIXELS),
            "-i", "pipe:0", *args]
    # The log's re-run, pasteable into a shell (a filter graph holds `;` and `[a]`): the private log may name both.
    by_hand = f"cd {shlex.quote(str(cwd))} && {shlex.join(argv)} < {shlex.quote(str(src))}"
    try:
        for final, _, _ in parts:
            core.private_dir(final.parent)
        # umask 077: ffmpeg's `.part` outputs are 600 from the first byte, as every other cache file.
        with open(src, "rb") as picture:
            out = subprocess.run(argv, stdin=picture, capture_output=True, cwd=cwd,
                                 timeout=max(0.2, float(timeout_s)), umask=0o077)
        ok = out.returncode == 0
        sizes = []
        for _, part, size in parts:
            st = part.stat() if part.exists() else None
            got = st.st_size if st is not None else None
            if st is None or st.st_size == 0 or (size is not None and st.st_size != size):
                ok = False
                sizes.append(f"{part.name}: want {size if size is not None else '>0'}, got {got}")
        if ok:
            for final, part, _ in parts:
                part.replace(final)
            return True
        if log is not None:
            err = out.stderr.decode("utf-8", "replace").strip()
            # ffmpeg names a picture past `-max_pixels` early in its stderr; the tail below would cut it.
            big = re.search(r"Picture size (\d+x\d+) exceeds specified max pixel count (\d+)", err)
            log.warn("thumbs", "ffmpeg", result="FAIL", rc=out.returncode, stderr=err[-200:],
                     sizes="; ".join(sizes), cmd=by_hand,
                     **({"picture": big.group(1), "max_pixels": int(big.group(2))} if big else {}),
                     suggest="the source image has more pixels than a still may; not converted" if big else
                     "run cmd by hand; a source ffmpeg cannot decode renders as no picture")
    except (OSError, subprocess.SubprocessError) as exc:
        if log is not None:
            log.warn("thumbs", "ffmpeg", result="FAIL", rc=None,
                     stderr=f"{type(exc).__name__}: {exc}"[-200:], sizes="", cmd=by_hand,
                     suggest="run cmd by hand; a timeout here means the deadline was already spent")
    for _, part, _ in parts:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
    return False


def convert_forms(src: pathlib.Path, media_id: int, episode: int, w: int, h: int,
                  timeout_s: float = FFMPEG_TIMEOUT_S, log: Log | None = None) -> bool:
    """<src> JPEG -> the crisp row buffer, the blurred row buffer and the blurred still JPEG, in ONE
    ffmpeg process. All three or none. Spawn cost dominates, so one process for three outputs is
    close to free: measured ~150 ms median here against ~144 ms for a single plain scale to
    rawvideo (module docstring has the run).

    `timeout_s` for a caller on a deadline (`cli.loading_art` runs between the claim and the
    resolve); see `_run_ffmpeg`."""
    crisp = path_for(media_id, episode, w, h)
    blur = path_for(media_id, episode, w, h, variant="blur")
    still = still_blur_path(media_id, episode)
    graph = (f"[0:v]scale={int(w)}:{int(h)},split=2[a][b];[b]gblur=sigma={_sigma(w)}[c];"
             f"[0:v]scale={STILL_W}:{STILL_H},gblur=sigma={_sigma(STILL_W)}[d]")
    args = ["-filter_complex", graph,
            "-map", "[a]", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgra", _part(crisp).name,
            "-map", "[c]", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgra", _part(blur).name,
            "-map", "[d]", "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-q:v", "3", _part(still).name]
    want = int(w) * int(h) * 4
    return _run_ffmpeg(src, args, [(crisp, want), (blur, want), (still, None)], timeout_s=timeout_s, log=log)


def convert_fanart_blur(src: pathlib.Path, media_id: int, timeout_s: float = FFMPEG_TIMEOUT_S,
                        log: Log | None = None) -> bool:
    dest = fanart_blur_path(media_id)
    args = ["-vf", f"scale={BACKDROP_W}:{BACKDROP_H},gblur=sigma={_sigma(BACKDROP_W)}",
            "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-q:v", "3", _part(dest).name]
    return _run_ffmpeg(src, args, [(dest, None)], timeout_s=timeout_s, log=log)


def convert_logo(src: pathlib.Path, media_id: int, w: int, h: int,
                 timeout_s: float = FFMPEG_TIMEOUT_S, log: Log | None = None) -> bool:
    """The clear logo letterboxed into w×h with a transparent margin, alpha PREMULTIPLIED as
    `overlay-add` requires ("every color component is already multiplied with the alpha")."""
    dest = logo_path(media_id, w, h)
    vf = (f"format=rgba,premultiply=inplace=1,scale={int(w)}:{int(h)}:force_original_aspect_ratio=decrease,"
          f"pad={int(w)}:{int(h)}:(ow-iw)/2:(oh-ih)/2:color=black@0.0")
    args = ["-vf", vf, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgra", _part(dest).name]
    return _run_ffmpeg(src, args, [(dest, int(w) * int(h) * 4)], timeout_s=timeout_s, log=log)


def make_backdrop(src_jpg, w: int, h: int, timeout_s: float = FFMPEG_TIMEOUT_S,
                  log: Log | None = None) -> str:
    """The one transient backdrop buffer, overwritten at every claim. "" when the source is missing.

    Centre-cropped to 64:27 before the scale, not scaled alone. A measurement fixed the
    Lua's destination rectangle as `dw = osd_w, dh = osd_h - band`: the band is 180 of the OSD's
    720 units, so for every 16:9 window the destination is 1280x540 -- 64:27, a 1.33x horizontal
    stretch of a 16:9 source. Scaling a 16:9 still straight into the buffer would therefore land on
    screen 33% too wide (everything in frame visibly fat). `crop=iw:iw*27/64` takes the rows off
    instead -- 27/64 is (9/16) x (540/720), i.e. a 16:9 frame minus the band -- so the picture that
    reaches the window has the aspect it was shot at. Full width is kept; only rows are cropped.

    The buffer itself stays `w x h` (960x540): it is the *squeeze* the Lua's stretch undoes, and
    the Lua reads its dimensions from the file name, so nothing downstream changes.

    A source wider than 64:27 (a banner, ~5.4:1) would make the crop taller than the frame and
    ffmpeg refuses it -- `_run_ffmpeg` then cleans up and this returns "", the same degradation as
    any other conversion failure. Every source this is called with is a 16:9 JPEG this module wrote
    itself (`still_blur_path` 640x360, `fanart_blur_path` 960x540), both forced by `scale=W:H`.

    `timeout_s` for a caller on a deadline (`cli.loading_art` runs between the claim and the
    resolve); see `_run_ffmpeg`.
    """
    src = pathlib.Path(src_jpg)
    if not _file_ok(src):
        return ""
    dest = backdrop_path(w, h)
    want = int(w) * int(h) * 4
    args = ["-vf", f"crop=iw:iw*27/64,scale={int(w)}:{int(h)}", "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "bgra", _part(dest).name]
    return str(dest) if _run_ffmpeg(src, args, [(dest, want)], timeout_s=timeout_s, log=log) else ""


def forms(media_id: int, episode: int, w: int, h: int) -> dict:
    """Which derived files exist for one episode, each key independently (no network, stat only)."""
    crisp, blur, still = path_for(media_id, episode, w, h), path_for(media_id, episode, w, h, "blur"), still_blur_path(media_id, episode)
    return {"thumb": str(crisp) if _bgra_ok(crisp, w, h) else "",
            "blurThumb": str(blur) if _bgra_ok(blur, w, h) else "",
            "blur": str(still) if _file_ok(still) else ""}


def ensure(http, log: Log, media_id: int, cells: list[dict], w: int, h: int,
           budget_s: float = 3.0, anchor: int | None = None, window: int = FETCH_WINDOW) -> dict[int, str]:
    """Make sure every cell with an image has a buffer on disk. Returns episode -> path.

    Never raises: a missing thumbnail must render as the text-only row the panel already draws,
    not as a failure.

    Fetches every still-needed URL through one `covers.fetch_many` call, not `covers.fetch_one` in
    a per-episode loop — see the module docstring for why a sequential fetch under a shared
    `budget_s` would leave most of a season without a picture. Conversion (ffmpeg on an
    already-local file) stays one episode at a time; only the network call is batched.

    One deadline (`t0 + budget_s`) covers the whole call, fetch and convert alike — not a budget
    split between the two phases. The fetch phase gets whatever time is left on that deadline when
    it starts; the conversion loop checks the same deadline before each item and stops, returning
    whatever it finished, once it passes. This is safe against the failure mode a per-phase split
    would exist to prevent — a rebuild whose fetch phase alone consumes the entire budget, starving
    conversion, on every rebuild, forever — because `covers.fetch_many` short-circuits on `have()`:
    a rebuild that spends its whole budget *downloading* leaves every one of those URLs cached, so
    the *next* rebuild's fetch phase for the same episodes is pure cache hits and costs almost
    nothing, leaving the entire next budget free to convert. Progress is guaranteed within two
    rebuilds, and a panel is rebuilt about twice per episode (see below), so the convergence
    horizon matches the refresh rate exactly. A permanent split would cap both phases forever to
    guard against a case that resolves itself in one pass. At the measured ~150 ms/conversion (see
    the module docstring) one 3 s budget converts roughly 20 episodes, so a full 49-cell window
    takes about three rebuilds to fill — the nearest-first order means the cells the cursor is on
    are in the first of those.

    Only the window around `anchor` is pending (see `fetch_order`); the rest is counted
    as `outside` and left for a rebuild whose anchor has moved.

    What "rebuilt" means here, since it bounds how much this function is worth optimising: a
    media's panel is rebuilt about twice per episode — once at play start (`do_play`), once at the
    watched threshold (`do_tick`, gated behind `if not decision["watched"]: return out`; `watched`
    is a one-shot, not a per-tick event) — plus once more on a player restart
    (`resume_into`, which passes no `http`; its caller attaches from `cached()`
    instead, so a restart still shows every thumbnail this function already fetched, it just can't
    fetch a *new* one there). Not once per tick during playback.
    """
    got: dict[int, str] = {}
    crisp_only: dict[int, str] = {}
    for cell in cells or []:
        try:
            episode = int(cell.get("episode"))
        except (TypeError, ValueError):
            continue
        f = forms(media_id, episode, w, h)
        if all(f.values()):
            got[episode] = f["thumb"]
        elif f["thumb"]:
            # The crisp buffer exists but the blur forms don't (a reconvert never finished, or an
            # older cache is missing them). Keep it as a fallback below -- a re-fetch that
            # fails, times out, or falls outside this call's anchor window must never make a
            # thumbnail this function already produced disappear from the return.
            crisp_only[episode] = f["thumb"]
    t0 = time.monotonic()
    deadline = t0 + max(0.0, float(budget_s))
    ordered, outside = fetch_order(cells, anchor, window)
    pending: list[tuple[int, str, pathlib.Path]] = []
    for cell in ordered:
        episode = int(cell["episode"])
        if episode in got:
            continue
        pending.append((episode, str(cell["image"]), path_for(media_id, episode, w, h)))

    fetched = failed = skipped = 0
    if pending:
        urls, seen = [], set()
        for _, url, _ in pending:
            if url not in seen:
                seen.add(url)
                urls.append(url)
        try:
            paths = covers.fetch_many(http, log, urls, budget_s=max(0.0, deadline - time.monotonic()))
        except Exception as exc:                        # noqa: BLE001 - a bug in the batch fetch
            # must degrade the whole batch here, not raise past this function's own contract.
            log.warn("thumbs", "fetch", result="FAIL", mediaId=media_id,
                     error=f"{type(exc).__name__}: {exc}")
            paths = {}
        for episode, url, dest in pending:
            if time.monotonic() >= deadline:
                # The same deadline the fetch phase used, not a separate conversion budget: see
                # the docstring for why this cannot starve forever. Whatever's left is simply not
                # attempted this call.
                skipped = len(pending) - fetched - failed
                break
            src = paths.get(url)
            if not src:
                # `covers.fetch_many`/`fetch_one` already logged the specific refusal (dead host,
                # bad status, wrong extension, oversized body, budget cutoff, ...); nothing more to
                # say here than "this one has no image".
                failed += 1
                continue
            if convert_forms(pathlib.Path(src), media_id, episode, w, h, log=log):
                got[episode] = str(dest)
                fetched += 1
            else:
                log.warn("thumbs", "convert", result="FAIL", episode=episode, src=src,
                         suggest="the row renders without a picture")
                failed += 1
    for episode, path in crisp_only.items():
        # A fresh convert_forms() above may have already replaced this episode's entry with a
        # full set of forms; setdefault only restores it when that never happened (skipped by the
        # deadline, outside this call's anchor window, or the reconvert itself failed).
        got.setdefault(episode, path)
    log.info("thumbs", "ensure", result="PASS", mediaId=media_id, size=f"{w}x{h}",
             have=len(got), fetched=fetched, failed=failed, skipped=skipped,
             anchor=anchor, window=window, outside=outside,
             ms=(time.monotonic() - t0) * 1000)
    return got


def ensure_show_art(http, log: Log, media_id: int, fanart_url: str, logo_url: str,
                    budget_s: float = 2.0, timeout_s: float = FFMPEG_TIMEOUT_S) -> dict:
    """Fanart and clear logo for one show, fetched through `covers` with the larger cap, then the
    blurred fanart JPEG and the premultiplied logo buffer. Never raises; every key "" on failure.

    `budget_s` bounds the *fetch* only; `timeout_s` is what the two conversions below get, for a
    caller on a deadline (`cli.loading_art` runs between the claim and the resolve). They are
    separate because the fetch is concurrent and interruptible and a conversion is neither: see
    `_run_ffmpeg`."""
    out = {"fanart": "", "fanartBlur": "", "logo": ""}
    t0 = time.monotonic()
    urls = [u for u in (str(fanart_url or ""), str(logo_url or "")) if u]
    if not urls:
        return out
    try:
        paths = covers.fetch_many(http, log, urls, budget_s=budget_s, max_bytes=SHOW_ART_MAX_BYTES)
    except Exception as exc:                        # noqa: BLE001
        log.warn("thumbs", "show-art", result="FAIL", mediaId=media_id, error=f"{type(exc).__name__}: {exc}")
        paths = {}
    fan = paths.get(str(fanart_url or ""), "")
    if fan:
        out["fanart"] = fan
        dest = fanart_blur_path(media_id)
        if _file_ok(dest) or convert_fanart_blur(pathlib.Path(fan), media_id, timeout_s=timeout_s, log=log):
            out["fanartBlur"] = str(dest)
    logo = paths.get(str(logo_url or ""), "")
    if logo:
        dest = logo_path(media_id, LOGO_W, LOGO_H)
        if _bgra_ok(dest, LOGO_W, LOGO_H) or convert_logo(pathlib.Path(logo), media_id, LOGO_W, LOGO_H,
                                                          timeout_s=timeout_s, log=log):
            out["logo"] = str(dest)
    log.info("thumbs", "show-art", result="PASS", mediaId=media_id, ms=(time.monotonic() - t0) * 1000,
             fanart=bool(out["fanart"]), fanartBlur=bool(out["fanartBlur"]), logo=bool(out["logo"]))
    return out


def attach(payload: dict, w: int, h: int) -> dict:
    """Fill every picture key a surface reads, from disk only (no network, no client). The
    payload's `image`, `fanartUrl` and `logoUrl` are URLs; the keys written are local paths or ""."""
    media_id = int(payload.get("mediaId") or 0)
    for cell in payload.get("episodes") or []:
        try:
            episode = int(cell.get("episode"))
        except (TypeError, ValueError):
            cell.update({"still": "", "thumb": "", "blurThumb": "", "blur": ""})
            continue
        url = str(cell.get("image") or "")
        cell["still"] = covers.have(url) if url else ""
        cell.update(forms(media_id, episode, w, h))
    fan = str(payload.get("fanartUrl") or "")
    payload["fanart"] = covers.have(fan) if fan else ""
    fb = fanart_blur_path(media_id)
    payload["fanartBlur"] = str(fb) if _file_ok(fb) else ""
    lg = logo_path(media_id, LOGO_W, LOGO_H)
    payload["logo"] = str(lg) if _bgra_ok(lg, LOGO_W, LOGO_H) else ""
    return payload


# ----------------------------------------------------------------------------- prune
PRUNE_MAX_AGE_DAYS = 30
PRUNE_MAX_BYTES = 256 * 1024 * 1024
PRUNE_EVERY_S = 86400
STAMP_NAME = ".pruned"
_SIZE_TAG = re.compile(r"@(\d+)x(\d+)(?:\.blur)?\.bgra$")


def current_sizes() -> set[tuple[int, int]]:
    return {(THUMB_W, THUMB_H), (LOGO_W, LOGO_H), (BACKDROP_W, BACKDROP_H)}


def _files() -> list[pathlib.Path]:
    try:
        return [p for p in THUMBS_DIR.rglob("*") if p.is_file() and not p.name.startswith(".")]
    except OSError:
        return []


def prune(max_age_days: float = PRUNE_MAX_AGE_DAYS, max_bytes: int = PRUNE_MAX_BYTES) -> dict:
    """Stale-size buffers first (unreadable forever), then files untouched for `max_age_days`, then,
    over `max_bytes`, least recently used first. Mirrors `covers.prune`."""
    t0 = time.monotonic()
    removed = stale = freed = 0
    cutoff = time.time() - float(max_age_days) * 86400
    sizes = current_sizes()
    alive: list[tuple[float, int, pathlib.Path]] = []
    for p in _files():
        try:
            st = p.stat()
        except OSError:
            continue
        m = _SIZE_TAG.search(p.name)
        if m and (int(m.group(1)), int(m.group(2))) not in sizes:
            try:
                p.unlink()
                stale += 1
                freed += st.st_size
                continue
            except OSError:
                pass
        if st.st_mtime < cutoff and st.st_atime < cutoff:
            try:
                p.unlink()
                removed += 1
                freed += st.st_size
                continue
            except OSError:
                pass
        alive.append((max(st.st_atime, st.st_mtime), st.st_size, p))
    total = sum(s for _, s, _ in alive)
    if total > max_bytes:
        for _, size, p in sorted(alive, key=lambda t: t[0]):
            if total <= max_bytes:
                break
            try:
                p.unlink()
            except OSError:
                continue
            removed += 1
            freed += size
            total -= size
    for d in [d for d in THUMBS_DIR.glob("*") if d.is_dir()] if THUMBS_DIR.exists() else []:
        try:
            d.rmdir()                         # only succeeds when empty
        except OSError:
            pass
    return {"removed": removed, "stale": stale, "freed": freed, "count": len(_files()), "bytes": total,
            "ms": round((time.monotonic() - t0) * 1000, 1)}


def maybe_prune(log: Log | None = None) -> dict | None:
    stamp = THUMBS_DIR / STAMP_NAME
    try:
        if time.time() - stamp.stat().st_mtime < PRUNE_EVERY_S:
            return None
    except OSError:
        pass
    core.touch_private(stamp)
    out = prune()
    if log is not None:
        log.info("thumbs.prune", "daily", result="PASS", **out)
    return out


def status() -> dict:
    files = _files()
    sizes = []
    for p in files:
        try:
            sizes.append((p.stat().st_mtime, p.stat().st_size))
        except OSError:
            continue
    return {"dir": str(THUMBS_DIR), "count": len(sizes), "bytes": sum(s for _, s in sizes),
            "oldest": min((m for m, _ in sizes), default=None), "newest": max((m for m, _ in sizes), default=None),
            "sizes": sorted(f"{w}x{h}" for w, h in current_sizes())}
