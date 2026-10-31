"""Player window control on Hyprland, now-playing state, the resume store and the watched rule.

The window is parked on `special:omaanime` and shown and hidden with the special workspace; a play resumes where it
stopped; an episode is marked watched once.
Every Hyprland call goes through `hyprctl` and is logged with its arguments, exit code and elapsed time, so a
failed show/hide is diagnosable from the trace alone.

Verified live on Hyprland 0.56.2 with a Lua config (2026-09-15):
- `hyprctl dispatch` in Lua mode rewrites its argument into `hl.dispatch(<arg>)`, so the classic
  `movetoworkspacesilent special:omaanime,pid:N` form is a Lua syntax error (exit 7). The Lua forms below are the
  ones Omarchy's own `omarchy-hyprland-window-pop` and `default/hypr/bindings/tiling.lua` use.
- `hl.window_rule(...)` at runtime is accepted but does not touch an already-mapped window, so float/size/center
  are dispatched directly by pid instead (8-13 ms each).
- `toggle_special` round trip: 25 ms show, 8 ms hide.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .anilist import next_episode, not_out, scheduled_episode
from .core import (APP_NAME, CACHE_DIR, MPV_SOCKET, PLAYER_LAUNCHER, RUNTIME_DIR, Log, StageError, cached_json,
                   LANGUAGE_VARIANTS, fallback_language, setting_variant, write_cache)
from .mpv import PlayerClosed

WINDOW_CLASS = f"{APP_NAME}-player"          # --wayland-app-id in bin/omarchy-anime-player
SPECIAL = APP_NAME                          # Hyprland special workspace name
# The window is sized from the monitor it is on, not from a constant. A hardcoded
# 1280x720 **logical** box does not fit a 1920x1080 panel at any scale above 1.13 — Hyprland's
# dispatchers speak logical pixels, so at scale 1.6 the screen is 1200x675 of them and the
# compositor simply clips whatever does not fit.
WINDOW_FILL = 0.9                          # of the usable area, leaving a visible border all round
WINDOW_ASPECT = 16 / 9
MIN_WINDOW = (320, 180)                    # floor: a bad monitor entry must not dispatch a 0x0 resize
NOW_PLAYING_FILE = CACHE_DIR / "now-playing.json"
EPISODES_DIR = CACHE_DIR / "episodes"     # <mediaId>.json, written by the CLI, read by the mpv script
MAPPINGS_DIR = CACHE_DIR / "mappings"     # ani.zip payloads, written by `anizip`, read here
RESUME_FILE = CACHE_DIR / "resume.json"
TAIL_FRACTION = 0.03                       # a stored position inside the last 3 % restarts from the top


# ----------------------------------------------------------------------------- window geometry

def usable_area(monitor: dict) -> tuple[float, float]:
    """The monitor's free **logical** area: pixels divided by scale, minus the reserved edges.

    `hyprctl monitors` reports `width`/`height` in physical pixels and `reserved` as
    `[left, top, right, bottom]` in logical ones; every `hl.dsp.window.*` dispatcher takes logical
    pixels. Measured on this machine: 1920x1080 at scale 1.6 with `reserved [0,24,0,0]` is
    1200x675 logical, 1200x651 usable.
    """
    monitor = monitor or {}
    try:
        scale = float(monitor.get("scale") or 1.0) or 1.0
    except (TypeError, ValueError):
        scale = 1.0
    reserved = list(monitor.get("reserved") or []) + [0, 0, 0, 0]
    try:
        left, top, right, bottom = (float(x or 0) for x in reserved[:4])
        w = float(monitor.get("width") or 0) / scale - left - right
        h = float(monitor.get("height") or 0) / scale - top - bottom
    except (TypeError, ValueError):
        return 0.0, 0.0
    return max(0.0, w), max(0.0, h)


def window_size(monitor: dict, fill: float = WINDOW_FILL, aspect: float = WINDOW_ASPECT) -> tuple[int, int]:
    """The largest `aspect` box inside `fill` of the monitor's usable logical area.

    eDP-1 here (1200x651 usable) -> 1040x585. A 1080p panel at scale 1 -> 1728x972.
    """
    uw, uh = usable_area(monitor)
    w, h = uw * float(fill), uh * float(fill)
    if w <= 0 or h <= 0:
        return MIN_WINDOW
    # Round the limiting dimension down first and derive the other from the *integer*, so the
    # dispatched pair is a true 16:9 rather than two independently truncated floats
    # (651 * 0.9 = 585.9 -> 585 x 1040, not 585 x 1041).
    if w / h > aspect:
        h = int(h)
        w = int(h * aspect)
    else:
        w = int(w)
        h = int(w / aspect)
    return max(MIN_WINDOW[0], w), max(MIN_WINDOW[1], h)


def fits(size, usable: tuple[float, float]) -> bool:
    """Does a window of this logical size fit the usable area? Unknown sizes do not."""
    try:
        return float(size[0]) <= usable[0] + 0.5 and float(size[1]) <= usable[1] + 0.5
    except (TypeError, ValueError, IndexError, KeyError):
        return False


def monitor_of(monitors: list, window: dict | None) -> dict | None:
    """The monitor the *window* is on, by `clients[].monitor` -> `monitors[].id`. Not the focused
    one: on a second screen the focused monitor's geometry is the wrong answer."""
    want = (window or {}).get("monitor")
    for m in monitors or []:
        if m.get("id") == want:
            return m
    return None


# ----------------------------------------------------------------------------- hyprctl

def hyprctl(log: Log, stage: str, args: list[str], timeout: float = 5.0) -> tuple[int, str]:
    """Run hyprctl and log the call. Returns (rc, combined output). rc is non-zero when Hyprland printed an
    `error:` line even if the process exited 0 (Lua-mode dispatch errors do that)."""
    t0 = time.monotonic()
    try:
        proc = subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise StageError(f"hypr.{stage}", "hyprctl not found", "the plugin needs a Hyprland session")
    except subprocess.TimeoutExpired:
        raise StageError(f"hypr.{stage}", f"hyprctl {' '.join(args[:2])} timed out after {timeout}s",
                         "is Hyprland responsive? try `hyprctl version`")
    out = (proc.stdout + proc.stderr).strip()
    rc = proc.returncode if not out.startswith("error") else (proc.returncode or 1)
    log.debug("hypr", stage, result="PASS" if rc == 0 else "FAIL", ms=(time.monotonic() - t0) * 1000,
              args=args[:2], rc=rc, out=out[:160])
    return rc, out


def hypr_json(log: Log, stage: str, what: str):
    rc, out = hyprctl(log, stage, ["-j", what])
    if rc != 0:
        raise StageError(f"hypr.{stage}", f"hyprctl -j {what} failed: {out[:120]}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise StageError(f"hypr.{stage}", f"hyprctl -j {what} returned invalid JSON: {exc}")


# A hyprlang config looks the Lua form's first word up as a dispatcher name and answers `Invalid dispatcher` with exit
# 0; a classic dispatcher answers `ok`, or its own error text, also with exit 0 (Hyprland 0.56.2,
# `src/debug/HyprCtl.cpp:1149-1157`; `hyprctl` exits 7 only on an `error:` answer, `hyprctl/src/main.cpp:277`).
REFUSED_DISPATCH = "Invalid dispatcher"


def dispatch(log: Log, stage: str, lua: str, classic: list[str]) -> str:
    """Lua form first, classic second (ini configs). Returns which form Hyprland accepted."""
    rc, out = hyprctl(log, stage, ["dispatch", lua])
    if rc == 0 and out != REFUSED_DISPATCH:
        return "lua"
    rc2, out2 = hyprctl(log, stage, ["dispatch", *classic])
    if rc2 == 0 and out2 == "ok":
        return "classic"
    raise StageError(f"hypr.{stage}", f"dispatch failed: {(out2 or out)[:160]}",
                     "run the dispatch by hand with `hyprctl dispatch`", lua=lua, classic=classic)


def find_window(log: Log, pid: int | None = None) -> dict | None:
    for c in hypr_json(log, "clients", "clients"):
        if c.get("class") == WINDOW_CLASS and (pid is None or c.get("pid") == pid):
            return c
    return None


def special_visible(log: Log) -> bool:
    return seen(None, hypr_json(log, "monitors", "monitors"))["specialShown"]


def window_summary(w: dict | None) -> dict | None:
    if not w:
        return None
    ws = (w.get("workspace") or {}).get("name")
    return {"address": w.get("address"), "pid": w.get("pid"), "workspace": ws, "floating": w.get("floating"),
            "size": w.get("size"), "parked": ws == f"special:{SPECIAL}"}


def park(log: Log, pid: int, wait: float = 5.0) -> dict:
    """Move mpv's window to special:omaanime, float it and size it. Idempotent."""
    t0 = time.monotonic()
    w = None
    while time.monotonic() - t0 < wait:
        w = find_window(log, pid)
        if w:
            break
        time.sleep(0.1)
    if not w:
        raise StageError("hypr.park", f"no {WINDOW_CLASS} window for pid {pid} within {wait}s",
                         "mpv must run with --force-window=immediate (bin/omarchy-anime-player)")
    before = window_summary(w)
    sel = f"pid:{pid}"
    # One extra `hyprctl -j monitors` (measured 3-5 ms): the size has to come from the monitor this
    # window is on, and `clients[]` carries only its index.
    mon = monitor_of(hypr_json(log, "monitors", "monitors"), w)
    usable = usable_area(mon) if mon else (0.0, 0.0)
    wanted = window_size(mon) if mon else None
    # Resizing only when the window arrived tiled made the fix dead code on every machine
    # where something else floats it first — which, with a leftover runtime `hl.window_rule` for
    # this class, is this one. Re-fit whenever the current size does not fit, and leave a window the user has
    # resized alone as long as it does.
    refit = wanted is not None and (not before["floating"] or not fits(before["size"], usable))
    actions = []
    if not before["parked"]:
        actions.append(("move", f'hl.dsp.window.move({{ window = "{sel}", workspace = "special:{SPECIAL}", follow = false }})',
                        ["movetoworkspacesilent", f"special:{SPECIAL},{sel}"]))
    if not before["floating"]:
        actions.append(("float", f'hl.dsp.window.float({{ window = "{sel}", action = "on" }})', ["setfloating", sel]))
    if refit:
        actions.append(("resize", f'hl.dsp.window.resize({{ window = "{sel}", x = {wanted[0]}, y = {wanted[1]} }})',
                        ["resizewindowpixel", f"exact {wanted[0]} {wanted[1]},{sel}"]))
        actions.append(("center", f'hl.dsp.window.center({{ window = "{sel}" }})', ["centerwindow", sel]))
    forms = {name: dispatch(log, f"park.{name}", lua, classic) for name, lua, classic in actions}
    after = window_summary(find_window(log, pid)) or before      # read the window back
    ok = after["parked"] and after["floating"] and fits(after["size"], usable)
    log.info("hypr", "park", result="PASS" if ok else "FAIL", ms=(time.monotonic() - t0) * 1000, pid=pid,
             before=before, after=after, forms=forms,
             monitor=(mon or {}).get("name"), scale=(mon or {}).get("scale"),
             usable=[round(usable[0], 1), round(usable[1], 1)], wanted=list(wanted) if wanted else None,
             applied=after["size"], refit=refit,
             suggest="" if ok else ("the window does not fit the monitor's usable logical area; compare "
                                    "`usable` with `applied`"))
    return after


# Hyprland 0.56.2 (`src/config/shared/actions/ConfigActions.cpp:1110-1137`): `toggle_special` acts on the *focused*
# monitor only. It closes the special when it is open there; otherwise `CMonitor::setSpecialWorkspace`
# (`src/output/Monitor.cpp:1552`) takes it from whichever monitor shows it and opens it on the focused one. So a hide
# that toggles while the special is shown on the other monitor moves it, still shown, to the one the viewer is at.
TOGGLE_SPECIAL = (f'hl.dsp.workspace.toggle_special("{SPECIAL}")', ["togglespecialworkspace", SPECIAL])
# Closing it on a given monitor instead: the Lua monitor object's `set_special_workspace({})`, an empty table, so its
# `workspace` field is nil and the monitor's special is cleared (`src/config/lua/objects/LuaMonitor.cpp:54-70`). With
# no argument at all `tableOptWorkspaceSelector` (`src/config/lua/bindings/LuaBindingsInternal.cpp:238-249`) indexes a
# missing value and raises. Run through `hyprctl eval` (`src/debug/HyprCtl.cpp:1110`, which answers exactly `ok`).
# The close moves keyboard focus to that monitor's last window (`Monitor.cpp:1580-1585`), so the same
# chunk puts it back with `hl.dsp.focus({ monitor = … })` (`tryMoveFocusToMonitor`, `ConfigActions.cpp:93-125`): the
# monitor's last-focused window, one cursor warp that `cursor:no_warps` suppresses, and nothing when focus never left.
CLOSE_SPECIAL_ON = ('local back = hl.get_active_monitor(); hl.get_monitor("{monitor}"):set_special_workspace({{}}); '
                    'if back and hl.get_active_monitor() ~= back then '
                    'hl.dispatch(hl.dsp.focus({{ monitor = back }})) end')
MONITOR_NAME = re.compile(r"[A-Za-z0-9._-]+")
# `hyprctl eval` on a hyprlang config (`src/debug/HyprCtl.cpp:1111-1112`). Any other answer than this and `ok` comes
# from a Lua config: `error: …` when the chunk raised, `warning: …` / `info: …` for an issue raised inside it, e.g.
# by the focus restore after the close went through (`src/config/lua/ConfigManager.cpp:928-944`).
NO_EVAL = "eval is only supported with the lua config manager"


def toggle_special(log: Log, stage: str) -> str:
    return dispatch(log, stage, *TOGGLE_SPECIAL)


def close_special(log: Log, stage: str, before: dict) -> str:
    """Close `special:omaanime` on the monitor that shows it. On the focused monitor that is the
    toggle. On another one the toggle would move it to the focused monitor, so it is closed there by `eval`, focus
    restored; a hyprlang config has no `eval` and gets focus there, toggle, focus back (two cursor warps)."""
    where, focused = before["specialOn"], before["focused"]
    if where is None or where == focused:
        return toggle_special(log, stage)
    if not MONITOR_NAME.fullmatch(str(where)):
        raise StageError(f"hypr.{stage}", f"refusing to put the monitor name {where!r} into Lua",
                         "close the special workspace by hand: `hyprctl dispatch togglespecialworkspace omaanime` "
                         "with that monitor focused")
    chunk = CLOSE_SPECIAL_ON.format(monitor=where)
    rc, out = hyprctl(log, stage, ["eval", chunk])
    if rc == 0 and out == "ok":
        return "eval"
    if rc == 0 and out.startswith(("warning:", "info:")):
        # The chunk ran to its end; an issue inside it (the restore's focus) is for the read-back to judge.
        log.warn("hypr", f"{stage}.eval", result="WARN", monitor=where, answer=out[:240],
                 suggest="the special was closed; `after.focused` in the `hypr` line says whether focus came back")
        return "eval"
    if out != NO_EVAL:
        raise StageError(f"hypr.{stage}", f"`hyprctl eval` could not close the special on {where}: {out[:160]}",
                         f"run it by hand: hyprctl eval '{chunk}'", eval=out[:240], monitor=where)
    classic = [["focusmonitor", where], ["togglespecialworkspace", SPECIAL]] + ([["focusmonitor", focused]]
                                                                                if focused else [])
    for args in classic:
        rc2, out2 = hyprctl(log, stage, ["dispatch", *args])
        if rc2 != 0 or out2 != "ok":
            raise StageError(f"hypr.{stage}", f"could not close the special on {where}: `{' '.join(args)}` "
                             f"answered {out2[:160]!r}",
                             "on hyprlang, focus that monitor and run `hyprctl dispatch togglespecialworkspace "
                             "omaanime`", eval=out[:160], dispatch=args)
    return "classic"


def _set_visible(log: Log, stage: str, want: bool) -> dict:
    """Shows or hides the special workspace alone: for a window `park` has just put there (a play, a restart).
    `set_window` is the viewer's show/hide/toggle and `stop`. Read back from `hyprctl monitors`."""
    t0 = time.monotonic()
    visible = special_visible(log)
    changed = False
    if want != visible:
        toggle_special(log, stage)
        changed = True
        visible = special_visible(log)
    ok = visible == want
    log.info("hypr", stage, result="PASS" if ok else "FAIL", ms=(time.monotonic() - t0) * 1000, visible=visible,
             changed=changed, suggest="" if ok else "special workspace did not toggle; is another special open?")
    return {"visible": visible, "changed": changed, "ms": round((time.monotonic() - t0) * 1000, 1)}


def seen(window: dict | None, monitors: list) -> dict:
    """Whether the viewer can see the player: parked with `special:omaanime` shown, or unparked on a
    workspace that some monitor shows. A window the viewer moved to workspace 2 (SUPER+SHIFT+2) is on screen while
    the special stays hidden. `clients[].visible` cannot decide it: Hyprland 0.56.2 reports `true` for a window on a
    workspace no monitor shows. With no window, the special being shown is all there is to see. `focused` and
    `specialOn` name the monitors, because `toggle_special` acts on the focused one (`TOGGLE_SPECIAL`)."""
    special = f"special:{SPECIAL}"
    special_on = next((m.get("name") for m in monitors or []
                       if (m.get("specialWorkspace") or {}).get("name") == special), None)
    w = window_summary(window)
    if w is None or w["parked"]:
        on = special_on
    else:
        on = next((m.get("name") for m in monitors or []
                   if (m.get("activeWorkspace") or {}).get("name") == w["workspace"]), None)
    return {"window": w is not None, "workspace": w and w["workspace"], "parked": bool(w and w["parked"]),
            "specialShown": special_on is not None, "specialOn": special_on,
            "focused": next((m.get("name") for m in monitors or [] if m.get("focused")), None),
            "on": on, "visible": on is not None}


def window_plan(before: dict, want: bool | None) -> dict:
    """The steps from `seen()`'s answer to the wanted state. want=None toggles what the viewer sees. Hiding with the
    special shown closes it first, where it is shown, so the window is never moved onto a shown special on its way
    out (no flash); an unparked window is parked either way; the special is shown last, on the focused monitor."""
    target = (not before["visible"]) if want is None else want
    steps = []
    if not target and before["specialShown"]:
        steps.append("hide_special")
    if (before["window"] and not before["parked"]) or (target and not before["window"]):
        steps.append("park")                    # with no window yet, `park` waits for a just-started one
    if target and not before["specialShown"]:
        steps.append("show_special")
    where = ("no window" if not before["window"] else "parked" if before["parked"]
             else f"on workspace {before['workspace']}")
    shown = f"on {before['on']}" if before["visible"] else "not on any monitor"
    special = f"shown on {before['specialOn']}" if before["specialShown"] else "hidden"
    why = (f"{where}, {shown}, special {special}, focus on {before['focused']}: "
           f"{'show' if target else 'hide'}" + (" (toggle)" if want is None else ""))
    return {"target": target, "steps": steps, "why": why}


def set_window(log: Log, stage: str, pid: int, want: bool | None) -> dict:
    """show/hide/toggle the player as the viewer sees it, parking an unparked window first. The decision
    is taken from one read of clients and monitors before anything moves; `_set_visible` alone decides from the
    special workspace, which is wrong once the window has left it. Reads both back afterwards, and logs
    the plan with the step that failed when one raises."""
    t0 = time.monotonic()
    before = seen(find_window(log, pid), hypr_json(log, "monitors", "monitors"))
    plan = window_plan(before, want)
    done, forms = [], {}
    try:
        for step in plan["steps"]:
            if step == "park":
                park(log, pid)
            elif step == "hide_special":
                forms[step] = close_special(log, stage, before)
            else:
                forms[step] = toggle_special(log, stage)
            done.append(step)
    except StageError as exc:
        log.emit("error", "hypr", stage, result="FAIL", ms=(time.monotonic() - t0) * 1000, pid=pid, before=before,
                 decision=plan["steps"], why=plan["why"], done=done, failed=plan["steps"][len(done)], forms=forms,
                 error=str(exc), data=exc.data,
                 suggest=exc.suggest or "see the `hypr` debug lines above for the dispatch's answer")
        raise
    after = seen(find_window(log, pid), hypr_json(log, "monitors", "monitors"))
    changed = any(before[k] != after[k] for k in ("workspace", "specialOn"))       # read, not planned
    ok = (after["visible"] == plan["target"] and (after["parked"] or not after["window"])
          and after["focused"] == before["focused"])
    log.info("hypr", stage, result="PASS" if ok else "FAIL", ms=(time.monotonic() - t0) * 1000, pid=pid,
             before=before, decision=plan["steps"], why=plan["why"], forms=forms, after=after,
             visible=after["visible"], changed=changed,
             suggest="" if ok else ("the player or the focus did not end up where it was sent; compare `before` and "
                                    "`after` (`specialOn`, `focused`) with `hyprctl -j clients` and `hyprctl -j "
                                    "monitors`"))
    return {"visible": after["visible"], "changed": changed, "parked": after["parked"],
            "ms": round((time.monotonic() - t0) * 1000, 1)}


def window_seen(log: Log, window: dict | None) -> bool:
    """`seen()` for a window already read, reading monitors only when there is one."""
    return bool(window) and seen(window, hypr_json(log, "monitors", "monitors"))["visible"]


def window_visible(log: Log, pid: int | None) -> bool:
    return window_seen(log, find_window(log, pid))


def show(log: Log) -> dict:
    return _set_visible(log, "show", True)


# --------------------------------------------------------------- process age and replacement
# mpv reads `--script=` once, at process start. A player that has been running since before the Lua
# script was last edited is therefore running the *previous* script out of memory, and every health
# signal the plugin has — the socket answers, the core responds, `user-data` says the script is
# loaded — reports success while half the feature is silently missing. The fix is to make process
# age a fact the plugin can state, and to own a real process replacement.
#
# Measured on this machine (2026-09-16), and the reason the two sources below are used in this order:
#   /proc/<pid>/stat field 22 + /proc/stat btime  -> 18:48:55.71   (`ps -o lstart` says 18:48:55)
#   os.stat("/proc/<pid>").st_mtime               -> 18:48:56.09   (0.4 s late, but never wrong by more)

SKEW_SECONDS = 2.0      # mtime vs. start-time jitter that must not read as staleness


def boot_time() -> float | None:
    """Epoch seconds at which this system booted, from `/proc/stat`'s `btime` line."""
    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def process_start_time(pid: int) -> float | None:
    """Epoch seconds at which `pid` started, or None when it cannot be read.

    `proc(5)`: field 22 of `/proc/<pid>/stat` is *starttime*, "the time the process started after
    system boot", in clock ticks. The comm field (2) is parenthesised and may contain spaces and
    `)`, so the split is anchored on the **last** `)`; after it, field 3 is index 0 and field 22 is
    index 19.
    """
    try:
        raw = Path("/proc", str(int(pid)), "stat").read_text(encoding="utf-8")
        ticks = float(raw.rsplit(")", 1)[1].split()[19])
        hz = float(os.sysconf("SC_CLK_TCK") or 100)
        btime = boot_time()
        if btime and hz > 0:
            return btime + ticks / hz
    except (OSError, ValueError, IndexError, TypeError):
        pass
    try:
        return Path("/proc", str(int(pid))).stat().st_mtime      # ~0.4 s late; still decisive
    except (OSError, ValueError, TypeError):
        return None


def process_cmdline(pid: int) -> list[str]:
    """The argv of `pid` as a list, or `[]`. NUL-separated with a trailing NUL, hence the strip."""
    try:
        raw = Path("/proc", str(int(pid)), "cmdline").read_bytes()
    except (OSError, ValueError, TypeError):
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def is_our_player(pid: int, socket_path: Path | str | None = None) -> bool:
    """Is `pid` an mpv serving *our* IPC socket? The guard before any signal is sent.

    The pid always comes from mpv's own `pid` property (or `Mpv._owner_pid`, which scans for the
    socket path); this confirms it against the process's own command line so a recycled pid can
    never be signalled. A pattern search (`pkill -f mpv`) is never used: the pattern matches this
    plugin's own shell and CLI processes too.
    """
    argv = process_cmdline(pid)
    if not argv:
        return False
    if "mpv" not in Path(argv[0]).name:
        return False
    needle = str(socket_path or MPV_SOCKET)
    return any(needle in arg for arg in argv[1:])


def startup_files(pid: int | None = None) -> list[Path]:
    """Every file the running player read at startup and cannot re-read: its `--script=` arguments
    and the launcher that built its command line.

    Taken from the **running process's own** cmdline rather than from this checkout's paths, because
    a player started from a different copy of the plugin (a repo tree vs. the installed one) loaded
    that copy's script, and that is the file whose age matters. `--script-opt` values are not
    included: they are strings mpv hands to the script, not files read at load time.
    """
    paths: list[Path] = []
    for arg in process_cmdline(pid) if pid else []:
        if arg.startswith("--script="):
            paths.append(Path(arg.split("=", 1)[1]))
    if PLAYER_LAUNCHER.exists():
        paths.append(PLAYER_LAUNCHER)      # a changed launcher changes the argv, which needs a restart too
    out: list[Path] = []
    for p in paths:
        try:
            resolved = p.resolve()
        except OSError:
            resolved = p
        if resolved not in out:
            out.append(resolved)
    return out


def stale_decision(started_at: float | None, files: list[dict], skew: float = SKEW_SECONDS) -> dict:
    """Pure: does any startup file post-date the process? `files` is `[{"path", "mtime"}, …]`.

    `skew` absorbs the sub-second disagreement between the two clocks involved (a file's mtime and a
    start time derived from boot time plus clock ticks); a file edited *during* the same second the
    player started is not evidence of anything either way.
    """
    out: dict = {"stale": False, "reason": "", "startedAt": started_at, "files": [], "newest": None}
    for f in files or []:
        mtime = f.get("mtime")
        newer = bool(started_at and mtime and float(mtime) > float(started_at) + float(skew))
        row = {"path": str(f.get("path")), "mtime": mtime, "newer": newer}
        out["files"].append(row)
        if newer and (out["newest"] is None or float(mtime) > float(out["newest"]["mtime"])):
            out["newest"] = row
    if started_at is None:
        out["reason"] = "the player's start time could not be read"
        return out
    if not out["files"]:
        out["reason"] = "no startup files to compare"
        return out
    if out["newest"]:
        out["stale"] = True
        name = Path(out["newest"]["path"]).name
        gap = float(out["newest"]["mtime"]) - float(started_at)
        out["reason"] = (f"the running player started {_stamp(started_at)}, "
                         f"{_duration(gap)} before {name} was last changed "
                         f"({_stamp(out['newest']['mtime'])}); it is still running the older copy")
    else:
        out["reason"] = f"the running player started {_stamp(started_at)}, after every file it loads"
    return out


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M:%S")


def _duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)} s"
    if seconds < 5400:
        return f"{int(round(seconds / 60))} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{int(seconds // 86400)} days"


def staleness(pid: int | None, log: Log | None = None) -> dict:
    """`stale_decision` over the real filesystem for a live pid. Never raises: it is on the status path."""
    if not pid:
        return {"stale": False, "reason": "no player is running", "startedAt": None, "files": [], "newest": None}
    files = []
    for path in startup_files(pid):
        try:
            files.append({"path": path, "mtime": path.stat().st_mtime})
        except OSError:
            files.append({"path": path, "mtime": None})
    out = stale_decision(process_start_time(pid), files)
    out["pid"] = int(pid)
    if log and out["stale"]:
        log.warn("player.stale", "check", result="FAIL", pid=int(pid), reason=out["reason"],
                 file=(out["newest"] or {}).get("path"),
                 suggest="mpv loads --script= once, at process start; `omarchy-anime player restart` "
                         "replaces this player and puts the episode back where it was")
    return out


def terminate(log: Log, pid: int, expect: Path | str | None = None,
              term_wait: float = 5.0, kill_wait: float = 3.0) -> dict:
    """Replace a player process: SIGTERM, then SIGKILL only after a bounded wait.

    Refuses any pid whose `/proc/<pid>/cmdline` is not an mpv serving `expect` — the pid is the
    player's own report of itself, and this is the independent confirmation before a signal.
    Verifies `/proc/<pid>` is actually gone before returning.
    """
    pid = int(pid)
    t0 = time.monotonic()
    if not is_our_player(pid, expect or MPV_SOCKET):
        raise StageError("player.stop", f"pid {pid} is not the plugin's mpv; refusing to signal it",
                         "check `omarchy-anime player status` and kill it by hand if it really is stuck",
                         argv=process_cmdline(pid)[:4])
    out = {"pid": pid, "signal": "SIGTERM", "gone": False, "ms": 0.0}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        out.update({"gone": True, "signal": "none", "note": "already gone"})
        log.info("player.stop", "terminate", result="PASS", **out)
        return out
    except PermissionError as exc:
        raise StageError("player.stop", f"not allowed to signal pid {pid}: {exc}",
                         "is that mpv running as another user?")
    for wait, sig in ((term_wait, None), (kill_wait, signal.SIGKILL)):
        if sig is not None:
            out["signal"] = "SIGKILL"
            log.warn("player.stop", "escalate", result="FAIL", pid=pid, waited=term_wait,
                     suggest="mpv did not exit on SIGTERM within the grace period; sending SIGKILL")
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not Path("/proc", str(pid)).exists():
                out.update({"gone": True, "ms": (time.monotonic() - t0) * 1000})
                log.info("player.stop", "terminate", result="PASS", **out)
                return out
            time.sleep(0.05)
    out["ms"] = (time.monotonic() - t0) * 1000
    log.error("player.stop", "terminate", result="FAIL", **out,
              suggest=f"pid {pid} survived SIGKILL; it is probably in uninterruptible IO")
    raise StageError("player.stop", f"mpv pid {pid} did not exit after SIGTERM and SIGKILL",
                     "the process is stuck in the kernel; `kill -9` will not help either")


# ----------------------------------------------------------------------------- now playing

def read_now_playing() -> dict | None:
    return cached_json(NOW_PLAYING_FILE, None)


def write_now_playing(data: dict) -> None:
    write_cache(NOW_PLAYING_FILE, data)


def clear_now_playing() -> None:
    NOW_PLAYING_FILE.unlink(missing_ok=True)


def advance_next(now_playing: dict, watched_episode: int, media_id: int | None = None) -> dict:
    """Recompute `next` after an episode is marked watched, using the AniList media snapshot stored at play time."""
    if media_id is not None and now_playing.get("mediaId") != media_id:
        return now_playing
    now_playing["next"] = next_episode(now_playing.get("media") or {}, watched_episode)
    return now_playing


# ----------------------------------------------------------------------------- what the player plays
# now-playing.json is written seconds after `loadfile` (the AniList and cover reads come first), and the
# service builds every tick from it. A tick in that gap named the previous episode with the new file's
# position. The play therefore stamps the player itself, just before `loadfile` (the player stamp), and a reader
# compares it with the episode it is about to write for. mpv keeps `user-data` across `loadfile` and answers
# `property not found` while nothing has been set (mpv 0.41, measured 2026-10-18).

PLAYING_KEY = "user-data/omaanime/playing"
STAMP_TIMEOUT_S = 2.0      # a tick holds the service's tick slot; a hung core must not hold it for the 10 s default


def playing_stamp(media_id, episode, info_hash) -> str:
    return f"{int(media_id)}:{int(episode)}:{str(info_hash or '').lower()}"


def _ascii_number(text: str) -> bool:
    """ASCII decimal digits only: `str.isdigit()` is True for `²`, which `int()` refuses."""
    return bool(text) and text.isascii() and text.isdecimal()


def parse_stamp(value) -> dict | None:
    """`mediaId:episode:infohash` -> its parts; None for anything else."""
    parts = str(value).split(":", 2) if isinstance(value, str) else []
    if len(parts) != 3 or not _ascii_number(parts[0]) or not _ascii_number(parts[1]):
        return None
    return {"mediaId": int(parts[0]), "episode": int(parts[1]), "infohash": parts[2].lower()}


def stamp_player(mpv, log: Log, stamp: str) -> bool:
    """Write the player stamp and read it back. Never raises: a play is not stopped by it, and the
    readers accept a missing stamp."""
    suggest = ("ticks for this play are not checked against the player; a switch may write the previous "
               "episode's resume position")
    if getattr(mpv, "sock", True) is None:
        log.warn("player.stamp", "set", result="FAIL", stamp=stamp, error="no player connection", suggest=suggest)
        return False
    try:
        mpv.command("set_property", PLAYING_KEY, stamp)
        readback = mpv.command("get_property", PLAYING_KEY, timeout=STAMP_TIMEOUT_S)
    except (StageError, OSError) as exc:
        log.warn("player.stamp", "set", result="FAIL", stamp=stamp, error=f"{type(exc).__name__}: {exc}",
                 suggest=suggest)
        return False
    ok = readback == stamp
    log.emit("info" if ok else "warn", "player.stamp", "set", result="PASS" if ok else "FAIL", stamp=stamp,
             readback=readback, suggest="" if ok else suggest)
    return ok


def read_stamp(mpv, log: Log | None = None) -> dict:
    """What the player says it is playing: {"state": "set"|"unset"|"unreadable", "stamp": parts or None}."""
    if mpv is None or getattr(mpv, "sock", True) is None:
        return {"state": "unreadable", "stamp": None, "error": "no player connection"}
    try:
        value = mpv.command("get_property", PLAYING_KEY, timeout=STAMP_TIMEOUT_S)
    except StageError as exc:
        if "property not found" in str(exc):
            return {"state": "unset", "stamp": None}
        return {"state": "unreadable", "stamp": None, "error": str(exc)}
    except OSError as exc:
        return {"state": "unreadable", "stamp": None, "error": f"{type(exc).__name__}: {exc}"}
    parsed = parse_stamp(value)
    return {"state": "set" if parsed else "unset", "stamp": parsed}


def episode_title(title, episode) -> str:
    """The title the player is forced to for one episode: `<title> - <episode>`, everywhere it is built."""
    return f"{title} - {episode}"


def prepare_load(mpv, log: Log, title: str, stamp: str | None) -> dict:
    """What every `loadfile` is preceded by. The forced title first, because until file-loaded mpv's
    media-title is the URL's last segment, and a TorBox URL carries the account key in its query (the window
    title and the service's `status` showed it). Then the player stamp, so a tick built from the previous
    episode's now-playing.json is dropped instead of written into that episode's resume entry. One policy for
    both: a refusal is logged and the load goes on; neither is worth failing a play over."""
    out = {"title": title, "titled": False, "stamped": False}
    try:
        mpv.command("set_property", "force-media-title", title)
        out["titled"] = True
    except (StageError, OSError) as exc:
        log.warn("player.load", "title", result="FAIL", title=title, error=f"{type(exc).__name__}: {exc}",
                 suggest="the loadfile's per-file options set the title too (`load_file`)")
    if parse_stamp(stamp):
        out["stamped"] = stamp_player(mpv, log, stamp)
    else:
        log.warn("player.load", "stamp", result="SKIP", reason="nothing says what is about to play",
                 suggest="ticks for this file are taken without the player stamp check")
    return out


def load_file(mpv, log: Log, url: str, title: str, start: float, stage: str = "mpv.loadfile",
              paused: bool = False) -> None:
    """`loadfile <url> replace -1 {options}` with the title in the per-file options.

    mpv 0.41's manual: the fourth argument is "a list of options and values which should be set while the
    file is playing"; over the client API "this can be a MPV_FORMAT_NODE_MAP", values strings, "restored to
    the previous value at end of playback". Measured on mpv 0.41 over loopback (2 runs each, 2026-10-18): with
    `force-media-title` in the map and no title forced before, `media-title` was the title from its first
    change on and never the URL, a comma in it included; `start` in the map seeked as the string form does.
    So the URL is never the title even if `prepare_load`'s `set_property` was refused. A refused map is a
    failed play with its cause, never a play that shows the URL."""
    options = {"force-media-title": str(title), "start": f"{float(start or 0):g}"}
    if paused:
        # Opens paused (a per-file option, so it does not outlive this file): ready with nobody watching.
        options["pause"] = "yes"
    # mpv's error lines on this connection until the wait for `file-loaded` ends them: the one place a refused
    # certificate is named (`mpv.tls_refusal`). Without them a refusal still fails the play, in general words.
    try:
        mpv.command("request_log_messages", "error")
    except PlayerClosed:
        raise
    except StageError as exc:
        log.warn(stage, "log_messages", result="FAIL", error=str(exc),
                 suggest="a refused certificate will read as any failed load; the play goes on")
    try:
        mpv.command("loadfile", url, "replace", -1, options)
    except PlayerClosed:
        raise                                           # the player went away: `do_play` says so in plain words
    except StageError as exc:
        log.error(stage, "options", result="FAIL", title=title, start=options["start"], error=str(exc),
                  suggest="mpv refused the per-file title; the play stops rather than show the stream's address")
        raise StageError(stage, f"mpv refused loadfile with its per-file options: {exc}",
                         "check that mpv is 0.38 or later (`mpv --version`)") from exc


def stamp_matches(read: dict, media_id, episode, info_hash=None) -> dict:
    """Pure. {"match": True|False|None, "reason"}. None: no player stamp to compare (a player from before the
    stamp, or unreachable) and the caller keeps its old behaviour. `info_hash` is compared only when given."""
    st = read.get("stamp")
    if read.get("state") != "set" or not st:
        return {"match": None, "reason": f"player stamp {read.get('state')}"}
    if st["mediaId"] != int(media_id) or st["episode"] != int(episode):
        return {"match": False, "reason": f"the player is on {st['mediaId']}:{st['episode']}, "
                                          f"not {int(media_id)}:{int(episode)}"}
    if info_hash and st["infohash"] and st["infohash"] != str(info_hash).lower():
        return {"match": False, "reason": "the player is on another release of this episode"}
    return {"match": True, "reason": "the player is on this episode"}


# ----------------------------------------------------------------------------- resume store

def _key(media_id: int, episode: int) -> str:
    return f"{int(media_id)}:{int(episode)}"


def _load_resume() -> dict:
    return cached_json(RESUME_FILE, None) or {}


def read_resume(media_id: int, episode: int) -> dict | None:
    return _load_resume().get(_key(media_id, episode))


def store_resume(media_id: int, episode: int, pos: float, dur: float, info_hash: str | None = None) -> dict:
    table = _load_resume()
    entry = table.get(_key(media_id, episode)) or {"watched": False}
    # A tick can fire in the gap where mpv has unloaded one file and not yet
    # loaded the next. It reports a real position and `duration` 0, and writing that zero destroys
    # a duration we already knew. A sample with no duration is not a sample of a duration; keep the
    # position, keep whatever length was last known.
    known = float(entry.get("dur") or 0)
    dur = float(dur or 0)
    entry.update({"pos": float(pos or 0), "dur": dur if dur > 0 else known,
                  "updatedAt": time.time()})
    # The release this position was watched in. A pick of another release of the same
    # episode must not seek into a different cut (`start_position`).
    if info_hash:
        entry["info_hash"] = str(info_hash).lower()
    table[_key(media_id, episode)] = entry
    write_cache(RESUME_FILE, table)
    return entry


CUT_SAME_S = 0.5           # two cut ticks this close are the player sitting at the same cut


def note_cut(media_id: int, episode: int, pos: float) -> bool:
    """True the first time a cut at this position is seen for the episode. With `--keep-open` the player
    sits at a cut and every 10 s tick carries `--eof` again; only the first one is news."""
    table = _load_resume()
    entry = table.get(_key(media_id, episode)) or {"watched": False}
    seen = entry.get("cutAt")
    if seen is not None and abs(float(seen) - float(pos or 0)) <= CUT_SAME_S:
        return False
    entry["cutAt"] = float(pos or 0)
    table[_key(media_id, episode)] = entry
    write_cache(RESUME_FILE, table)
    return True


def mark_watched(media_id: int, episode: int) -> dict:
    table = _load_resume()
    entry = table.get(_key(media_id, episode)) or {"pos": 0.0, "dur": 0.0}
    entry.update({"watched": True, "watchedAt": time.time()})
    table[_key(media_id, episode)] = entry
    write_cache(RESUME_FILE, table)
    return entry


def start_position(media_id: int, episode: int, info_hash: str | None = None) -> float:
    """Seconds to pass as mpv `start=`. 0 when unknown, already watched, inside the final 3 %, or
    saved while a different release of the episode was playing. A position saved before
    releases were recorded carries no hash and still resumes: nothing says it is wrong."""
    entry = read_resume(media_id, episode)
    if not entry or entry.get("watched"):
        return 0.0
    saved = entry.get("info_hash")
    if info_hash and saved and str(saved).lower() != str(info_hash).lower():
        return 0.0
    pos, dur = float(entry.get("pos") or 0), float(entry.get("dur") or 0)
    if dur > 0 and pos >= dur * (1 - TAIL_FRACTION):
        return 0.0
    return pos


# ----------------------------------------------------------------------------- watched rule

def clamp_threshold(threshold_pct) -> int:
    """`watchedThreshold` within 50-100; a typo in config must not mark every episode at 5 %."""
    return min(100, max(50, int(threshold_pct or 85)))


EOF_END_FRACTION = 0.95    # an eof at or past this share of the duration is the end of the file
EOF_END_BYTES = 0.999      # or at or past this share of a known file size read,
EOF_BYTES_MIN_TIME = 0.75  # and then only at or past this share of the duration


def eof_is_end(pos: float, dur: float, threshold_pct: int, stream_pos=None, file_size=None) -> dict:
    """Pure. mpv reports `eof-reached` for a stream that was cut as well as for one that
    ended. Measured on mpv 0.41 with the player's cache flags over loopback HTTP: a server that stopped
    at 30 % gave eof-reached at time-pos 11.68 of 40.02 s; the whole file gave it at 39.96 (99.84 %).

    **Time.** An eof is the end at or past `min(threshold, EOF_END_FRACTION)`. At the default 85 that is
    the threshold itself, so any eof from 85 % up counts (a cut in the last 15 % included, as the 85 %
    tick has already counted the episode). The floor matters for a threshold above 95: 100 is never
    reached by a tick (time-pos stops short of the duration), and the bound is that a real end
    at 96 % counts.

    **Bytes.** A container can declare a duration longer than its audio and video: a subtitle event past
    the end (ffmpeg muxed `Duration` 50.0 for a 40 s file) or a patched header both ended at 80 % by time.
    Then the bytes say it: at every real mkv end measured, `stream-pos` was 99.96-99.97 % of `file-size`.
    What refuses a cut is that ratio, not a lost size: a reconnect answered 500, 416 or refused leaves
    `file-size` 0 or unavailable, but a server that closes a `206` cleanly keeps it (measured
    8 of 8 runs), and such a cut at 30 % reads 29.92 %, at 99.5 % of a long-declared file 99.47 %.
    So an eof is also the end when `stream-pos` is at or past `EOF_END_BYTES` (99.9 %) of a known, non-zero
    `file-size`. `stream-pos` is the last packet passed to a decoder (mpv's manual), so the gap at a real end
    is that packet plus any trailing index.

    **A floor by time under the bytes**. A file whose bytes stop early but whose
    header keeps the full duration reads about 100 % of its bytes wherever it stops (measured: 99.74 % at
    29 % of the time). The bytes alone cannot tell it from a long declared duration, whose measured ends
    sat at 79.5-80 % by time, so the byte rule counts only at or past `EOF_BYTES_MIN_TIME` (75 %). An AniList
    write cannot be taken back, so the doubtful side is the cut.

    No known duration is below every floor: a cut, whatever the bytes."""
    dur = float(dur or 0)
    floor = min(clamp_threshold(threshold_pct) / 100, EOF_END_FRACTION)
    ratio = float(pos or 0) / dur if dur > 0 else 0.0
    size = float(file_size) if isinstance(file_size, (int, float)) and not isinstance(file_size, bool) else 0.0
    read = float(stream_pos) if isinstance(stream_pos, (int, float)) and not isinstance(stream_pos, bool) else None
    out = {"ratio": ratio, "floor": floor, "bytes": [read, size or None]}
    if dur > 0 and ratio >= floor:
        return {**out, "end": True, "by": "time", "why": "eof at the end of the file"}
    if dur > 0 and ratio >= EOF_BYTES_MIN_TIME and size > 0 and read is not None and read >= size * EOF_END_BYTES:
        return {**out, "end": True, "by": "bytes",
                "why": f"eof at {ratio:.0%} by time, but {read:.0f} of {size:.0f} bytes read "
                       f"({read / size:.2%}): the declared duration runs long"}
    if dur <= 0:
        return {**out, "end": False, "by": None, "why": "eof with no known duration"}
    unknown = "" if size > 0 else "; the file size was not known, so the bytes could not be checked"
    return {**out, "end": False, "by": None,
            "why": f"eof at {ratio:.0%}, short of {floor:.0%}: the stream was cut, not finished{unknown}"}


def watched_decision(entry: dict | None, pos: float, dur: float, threshold_pct: int, eof: bool = False,
                     stream_pos=None, file_size=None) -> dict:
    """Pure. `watched` is True only the first time the rule fires for this (mediaId, episode).
    `cut` is True for an eof short of the end: not watched, and nothing advances on it."""
    ratio = (float(pos) / float(dur)) if dur and float(dur) > 0 else 0.0
    end = eof_is_end(pos, dur, threshold_pct, stream_pos, file_size) if eof else None
    cut = bool(end and not end["end"])
    if entry and entry.get("watched"):
        return {"watched": False, "ratio": ratio, "reason": "already", "cut": cut,
                **({"why": end["why"]} if cut else {})}
    pct = clamp_threshold(threshold_pct)
    if dur and float(dur) > 0 and ratio >= pct / 100:
        return {"watched": True, "ratio": ratio, "reason": "threshold", "cut": cut}
    if end and end["end"]:
        return {"watched": True, "ratio": ratio, "reason": "eof", "cut": False, "end_by": end["by"],
                **({"why": end["why"]} if end["by"] == "bytes" else {})}
    if cut:
        return {"watched": False, "ratio": ratio, "reason": "eof_cut", "cut": True, "why": end["why"]}
    return {"watched": False, "ratio": ratio, "reason": "below", "cut": False}


# ----------------------------------------------------------------------------- episode list
# The in-player episode panel's data. Everything here is a **local derivation** of what
# `now-playing.json` already holds plus the ani.zip mapping already on disk: no AniList call, no
# index call, nothing on the play path but a file write.

def _last_aired_episode(media: dict | None):
    """`nextAiringEpisode.episode` is the first *un*aired one, so the last aired is one before it. An entry that is not
    out (`anilist.not_out`) has none aired: 0."""
    if not media:
        return None
    scheduled = scheduled_episode(media)
    if scheduled:
        return scheduled - 1
    if not_out(media):
        return 0
    try:
        return int(media["episodes"]) if media.get("episodes") else None
    except (OverflowError, TypeError, ValueError):     # `json.loads` reads `Infinity`, and int() of it raises
        return None


# The most cells an episode list builds: `BrowseModel.js` `EPISODE_CELLS_MAX`, the same number. The count is
# AniList's, and 2^31 - 1 cells hung the CLI; the longest recorded show had 1,181 episodes (its next airing one).
EPISODE_CELLS_MAX = 10000


def episode_window(media: dict | None, progress: int = 0, anchor: int | None = None) -> tuple[int, int, int]:
    """(first, last, count) of the episodes the cells cover, `BrowseModel.js:episodeWindow`'s port. Above
    `EPISODE_CELLS_MAX`, the window of that many around `anchor` (default the next episode), inside 1..count."""
    if not media:
        return 1, 0, 0
    progress = max(0, int(progress or 0))
    last_aired = _last_aired_episode(media)
    # Not out with no count: no guess at one (24 unaired cells said nothing); the watched ones only.
    try:
        count = int(media.get("episodes") or 0) or last_aired or (progress if not_out(media) else progress + 24)
    except (OverflowError, TypeError, ValueError):     # `Infinity`, as JavaScript's `isFinite` guard reads it
        count = 2 ** 53 - 1
    if not count or count <= 0:
        return 1, 0, 0
    count = int(count)
    if count <= EPISODE_CELLS_MAX:
        return 1, count, count
    at = int(anchor) if anchor and int(anchor) > 0 else progress + 1
    first = max(1, min(at - EPISODE_CELLS_MAX // 2, count - EPISODE_CELLS_MAX + 1))
    return first, first + EPISODE_CELLS_MAX - 1, count


def episode_cells_note(media: dict | None, progress: int = 0, anchor: int | None = None) -> str:
    """"" when every episode has a cell; else `BrowseModel.js:episodeCellsNote`'s line."""
    first, last, count = episode_window(media, progress, anchor)
    if count <= EPISODE_CELLS_MAX:
        return ""
    return f"Episodes {first}\u2013{last} of {count} shown \u00b7 {count - (last - first + 1)} more not listed"


def episode_cells(media: dict | None, progress: int = 0, anchor: int | None = None) -> list[dict]:
    """One cell per episode: `watched` / `next` / `aired` / `unaired`, at most `EPISODE_CELLS_MAX` (`episode_window`).

    A port of `plugin/BrowseModel.js:episodeCells` (plus `anchor`: the episode playing, which the grid has no
    notion of), which the Browse overlay's grid already uses, so the panel and the grid cannot disagree about what
    is watched. The two are pinned to the same expectations by one set of test cases, which the Python and the Node
    tests both read.
    """
    if not media:
        return []
    progress = max(0, int(progress or 0))
    last_aired = _last_aired_episode(media)
    first, last, _ = episode_window(media, progress, anchor)
    cells = []
    for ep in range(first, last + 1):
        if ep <= progress:
            state = "watched"
        elif last_aired is not None and ep > last_aired:
            state = "unaired"
        elif ep == progress + 1:
            state = "next"
        else:
            state = "aired"
        cells.append({"episode": ep, "state": state})
    return cells


def episode_meta(media_id: int, log: Log | None = None) -> dict[int, dict]:
    """Per-episode title, image URL and runtime from the cached ani.zip mapping.

    Everything here is already on disk — the mapping is fetched on every resolve — so the rich
    episode panel costs no network call.

    This sits in the play-start path (`refresh_episode_panel` calls it inline, with mpv already
    attached), so a malformed mapping — a wrong-typed `episodes`, a per-episode entry that isn't a
    dict, a `title` that isn't a dict, an upstream ani.zip schema change, even a non-coercible
    `media_id` — must never raise past here (Rule: no failure in a polish path may fail a play). The
    guard holds on this function's own contract, not by luck of the call graph: `path` is built and
    the file read *inside* the `try`, not above it. One `try` wraps the whole per-file walk rather
    than an isinstance check at every level: whatever entries were parsed before the bad one are kept
    and returned, and the failure is logged with the file and the exception, because a silent
    empty return on a corrupt cache would be undiagnosable — but losing that log line is itself
    better than losing the play, so the log call is guarded too.
    """
    out: dict[int, dict] = {}
    path = None
    try:
        path = MAPPINGS_DIR / f"{int(media_id)}.json"
        mapping = cached_json(path, None) or {}
        for key, ep in (mapping.get("episodes") or {}).items():
            try:
                n = int(key)
            except (TypeError, ValueError):
                continue
            title = (ep or {}).get("title") or {}
            best = title.get("en") or title.get("x-jat") or next((v for v in title.values() if v), None)
            try:
                runtime = int((ep or {}).get("runtime") or 0) or None
            except (TypeError, ValueError):
                runtime = None
            out[n] = {"title": str(best or ""), "image": str((ep or {}).get("image") or ""),
                      "runtime": runtime}
    except Exception as exc:                   # noqa: BLE001 - a corrupt/schema-changed mapping must
        # degrade to whatever was already parsed, never fail the play.
        if log is not None:
            try:
                log.warn("episodes", "meta", result="FAIL", mediaId=media_id, path=str(path),
                         error=f"{type(exc).__name__}: {exc}",
                         suggest="the panel will show text-only cells for this episode")
            except Exception:                  # noqa: BLE001 - losing a log line beats losing the play
                pass
    return out


_STREAM_EP = re.compile(r"^\s*Episode\s+(\d+)\b")


def episode_stills(media: dict | None) -> dict[int, str]:
    """Episode number -> thumbnail URL from AniList's `streamingEpisodes`. Pure;
    never raises on any shape AniList (or a test) hands over. Only entries titled `Episode N …` count."""
    out: dict[int, str] = {}
    items = (media or {}).get("streamingEpisodes") if isinstance(media, dict) else None
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        m = _STREAM_EP.match(str(item.get("title") or ""))
        url = str(item.get("thumbnail") or "")
        if m and url:
            out.setdefault(int(m.group(1)), url)
    return out


_IMAGE_KINDS = {"Fanart": "fanart", "Clearlogo": "logo", "Poster": "poster", "Banner": "banner"}


def show_images(media_id, log: Log | None = None) -> dict:
    """The show-level art URLs from the cached ani.zip mapping's `images[]`.
    Every key present, "" when absent; never raises."""
    out = {v: "" for v in _IMAGE_KINDS.values()}
    try:
        mapping = cached_json(MAPPINGS_DIR / f"{int(media_id)}.json", None) or {}
        for item in mapping.get("images") or []:
            if not isinstance(item, dict):
                continue
            kind = _IMAGE_KINDS.get(str(item.get("coverType") or ""))
            url = str(item.get("url") or "")
            if kind and url and not out[kind]:
                out[kind] = url
    except Exception as exc:                   # noqa: BLE001
        if log is not None:
            try:
                log.warn("episodes", "images", result="FAIL", mediaId=media_id, error=f"{type(exc).__name__}: {exc}")
            except Exception:                  # noqa: BLE001
                pass
    return out


def resume_fractions(media_id) -> dict[int, float]:
    """episode -> pos/dur for every unwatched, unfinished episode of one show, from one read of
    the resume store. `dur > 0` is not hypothetical: a tick with no duration once zeroed a stored one,
    and that damage (dur 0) is still in the file."""
    out: dict[int, float] = {}
    prefix = f"{int(media_id)}:"
    for key, entry in _load_resume().items():
        if not str(key).startswith(prefix) or not isinstance(entry, dict) or entry.get("watched"):
            continue
        try:
            episode = int(str(key)[len(prefix):])
            pos, dur = float(entry.get("pos") or 0), float(entry.get("dur") or 0)
        except (TypeError, ValueError):
            continue
        if dur <= 0 or pos <= 0 or pos >= dur * (1 - TAIL_FRACTION):
            continue
        out[episode] = round(min(pos / dur, 1.0), 4)
    return out


def build_episodes(media: dict | None, progress: int, playing: int | None = None,
                    log: Log | None = None) -> dict:
    """The sidecar payload. Pure apart from the on-disk title/image/runtime/resume/show-art lookup."""
    media = media or {}
    media_id = int(media.get("id") or 0)
    meta = episode_meta(media_id, log) if media_id else {}
    cells = episode_cells(media, progress, playing)
    note = episode_cells_note(media, progress, playing)
    if note and log is not None:
        first, last, count = episode_window(media, progress, playing)
        log.warn("episodes.cells", "cut", result="PARTIAL", media_id=media_id, count=count, limit=EPISODE_CELLS_MAX,
                 first=first, last=last, suggest="AniList names more episodes than any show has; the list shows the "
                 "ones around the next episode")
    stills = episode_stills(media)
    fractions = resume_fractions(media_id) if media_id else {}
    images = show_images(media_id, log) if media_id else {"fanart": "", "logo": ""}
    for c in cells:
        c["image"] = stills.get(c["episode"], "")
        c["runtime"] = None
        c["resume"] = fractions.get(c["episode"])
        m = meta.get(c["episode"])
        if not m:
            continue
        if m["title"]:
            c["title"] = m["title"]
        if m["image"]:
            c["image"] = m["image"]
        c["runtime"] = m["runtime"]
    title = (media.get("title") or {})
    return {
        "mediaId": media_id,
        "title": str(title.get("english") or title.get("romaji") or title.get("native") or ""),
        "progress": max(0, int(progress or 0)),
        "playing": int(playing) if playing else None,
        "updatedAt": time.time(),
        "episodes": cells,
        # "" unless the cells stop short of the count (`episode_cells_note`).
        "note": note,
        "fanartUrl": images["fanart"],
        "logoUrl": images["logo"],
    }


def episodes_path(media_id) -> Path:
    """`<EPISODES_DIR>/<mediaId>.json`. The id is forced through `int()` first: it reaches here from
    a payload, and a path segment built from untrusted input is how a cache directory escapes."""
    return EPISODES_DIR / f"{int(media_id)}.json"


def write_episodes(media_id, payload: dict) -> Path:
    path = episodes_path(media_id)
    write_cache(path, payload)
    return path


def read_episodes(media_id) -> dict | None:
    try:
        return cached_json(episodes_path(media_id), None)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------- picker key
# A play refused inside the player (`cli.lend_picker_key`). The player stays where it is, its red line says so
# (`detail.picker` on the failed entry), and `p` can open the release picker. `p` is borrowed from whatever it did
# (mpv's own default is `cycle pause`) and the next play gives it back, so the viewer's key map is never changed for
# good.
# What a press does is decided at the press, by the player's script (`picker-key`): the picker only while nothing is
# watched and the entry still says it lent `p`; over an episode being watched (a paused one in a hidden window, an
# ended one sought back) `p` does what it did, the saved command, quoted as one word (mpv's double quotes read JSON
# escapes). The command holds no word of the show (a command line is readable by every account): the Service reads the
# refused play from `status` (`refusedPicker`). `media_id` and `episode` are for the log.
PICKER_KEY = "p"
PICKER_KEY_FILE = RUNTIME_DIR / f"{APP_NAME}-picker-key.json"
_PICKER_CMD = "script-message-to omarchy_anime_episodes picker-key"
# The borrow's own bindings, this one's and the ones before it: never saved as what `p` did.
_PICKER_OWN = (_PICKER_CMD, f"run omarchy-shell {APP_NAME} ")
# Set by the player's script on load: it handles `picker-key`. A player started before its script had it (mpv reads
# `--script=` once, at start) or one without the script answers "property not found" (probed on mpv 0.41, both), and
# there a `p` bound to the message would do nothing at all, not even pause: nothing is lent.
PICKER_READY = "user-data/omarchy-anime/picker-key"


def picker_binding(restore: str) -> str:
    return f"{_PICKER_CMD} {json.dumps(str(restore), ensure_ascii=False)}"


def picker_key_ready(mpv) -> bool:
    try:
        return mpv.command("get_property", PICKER_READY) is True
    except StageError as exc:
        if "property not found" not in str(exc):
            raise                            # no reply, a closed player: the lend's FAIL line says which
        return False


def arm_picker_key(mpv, log: Log, media_id: int, episode: int, stage: str = "") -> dict:
    if not picker_key_ready(mpv):
        log.info("player.picker_key", "arm", result="SKIP", stage_failed=stage, mediaId=int(media_id),
                 episode=int(episode), key=PICKER_KEY, ready=PICKER_READY,
                 reason="the running player predates its script: it cannot handle the press",
                 suggest="`omarchy-anime player restart` loads it; until then p stays what it was and the popout's "
                         "Choose a release opens the picker")
        return {"armed": False, "key": PICKER_KEY}
    saved = cached_json(PICKER_KEY_FILE, None)
    if not isinstance(saved, dict):
        bindings = mpv.command("get_property", "input-bindings") or []
        mine = [b for b in bindings if b.get("key") == PICKER_KEY
                and not str(b.get("cmd") or "").startswith(_PICKER_OWN)]
        top = max(mine, key=lambda b: b.get("priority") or 0, default=None)
        saved = {"key": PICKER_KEY, "cmd": (top or {}).get("cmd") or "ignore"}
        write_cache(PICKER_KEY_FILE, saved)
    bound = picker_binding(saved["cmd"])
    mpv.command("keybind", PICKER_KEY, bound)
    log.info("player.picker_key", "arm", result="PASS", stage_failed=stage, mediaId=int(media_id),
             episode=int(episode), key=PICKER_KEY, cmd=bound, restores=saved["cmd"])
    return {"armed": True, "key": PICKER_KEY, "restores": saved["cmd"]}


def disarm_picker_key(mpv, log: Log) -> bool:
    saved = cached_json(PICKER_KEY_FILE, None)
    if not isinstance(saved, dict):
        return False
    mpv.command("keybind", saved.get("key") or PICKER_KEY, saved.get("cmd") or "ignore")
    PICKER_KEY_FILE.unlink(missing_ok=True)
    log.info("player.picker_key", "disarm", result="PASS", key=saved.get("key"), restored=saved.get("cmd"))
    return True


# ----------------------------------------------------------------------------- subtitle font
# The font a subtitle falls back to when its own style names one that is missing (anime ASS files
# routinely do). Empty `subtitleFont` follows `subtitleLanguage`. Each script's fallback was measured
# under `sans-serif` (`fc-match 'sans-serif:charset=<letter>'`) and its vertical extent, which libass scales to `\fs`.
# A language gets a face where the fallback draws its letters small or in another region's glyphs:
# - Arabic and Persian: Noto Naskh Arabic (extent 2.04 em) -> its UI variant (1.36);
# - Hindi: Noto Sans Devanagari (1.91) -> its UI variant (1.36), the same step;
# - Chinese and Japanese: `sans-serif` gives Han letters from Noto Sans CJK KR, the Korean forms -> SC and JP.
# Thai (Noto Sans Thai, 1.51, no UI variant installed), Sinhala (1.31), Korean (CJK KR) keep the fallback; Latin,
# Cyrillic, Greek, Hebrew and Vietnamese are in `sans-serif`'s own face (Liberation Sans, 1.12).
MPV_DEFAULT_SUB_FONT = "sans-serif"           # `man mpv`, --sub-font: "The default is sans-serif"
LANGUAGE_FONTS = {"ara": "Noto Naskh Arabic UI", "per": "Noto Naskh Arabic UI", "hin": "Noto Sans Devanagari UI",
                  "chi": "Noto Sans CJK SC", "jpn": "Noto Sans CJK JP"}
# fontconfig's core generic families (the three `49-sansserif.conf` tests with `not_eq`). fc-match
# resolves them to a real face and never echoes the name, so no query can tell them from a missing font.
# `bin/omarchy-anime-player` holds a bash copy of this set (`@(sans-serif|serif|monospace)`); keep the two equal.
GENERIC_FAMILIES = frozenset({"sans-serif", "serif", "monospace"})


def subtitle_font(settings: dict) -> str:
    typed = str(settings.get("subtitleFont") or "").strip()
    if typed:
        return typed
    return LANGUAGE_FONTS.get(str(settings.get("subtitleLanguage") or "").lower(), MPV_DEFAULT_SUB_FONT)


# One of three copies of the installed-font rule: bin/omarchy-anime-player (at launch) and the panel's
# detect_arabic_font (mpv/omarchy-anime-episodes.lua) hold the other two.
def font_installed(family: str) -> bool:
    """fontconfig answers every query with *some* font; installed means it answered with this one,
    or the name is a generic family, which fontconfig always resolves."""
    if family.strip().lower() in GENERIC_FAMILIES:
        return True
    try:
        out = subprocess.run(["fc-match", "-f", "%{family}", family], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return family.lower() in (out.stdout or "").lower()


def variant_langs(code: str, variant: str | None) -> list[str]:
    """One language's `slang` entries: the table code alone, or for a chosen variant the variant, its
    equivalent tags, the code, then the language's other variants and theirs. mpv 0.41's `mp_match_lang` scores a
    track whose region an entry misses 1000 below one it matches, and a later entry only 1 below, so a variant
    listed by name beats the second language's bare tag (`pt-PT` before `en` for a `pt-BR` viewer)."""
    if not variant:
        return [code]
    def tags(v):
        return [v, *LANGUAGE_VARIANTS[v][2]]
    others = [t for v, (base, _, _) in LANGUAGE_VARIANTS.items() if base == code and v != variant for t in tags(v)]
    return [*tags(variant), code, *others]


def subtitle_langs(settings: dict) -> list[str]:
    """The subtitle languages mpv picks a track by at load (`slang`), in order: the viewer's, then
    the second language (`subtitleFallbackLanguage`; none for `off` or when it is the viewer's), each with its
    chosen variant's tags (`variant_langs`)."""
    lang = str(settings.get("subtitleLanguage") or "")
    second = fallback_language(settings)
    out = variant_langs(lang, setting_variant(settings))
    if second and second != lang:
        out += variant_langs(second, setting_variant(settings, "subtitleFallbackLanguage"))
    return out


def apply_subtitle_langs(mpv, log: Log, settings: dict) -> list[str]:
    """Set the running player's `slang` for this play, as `apply_subtitle_font` sets `sub-font`: the launcher's
    `--slang` is only the first play's. mpv 0.41 takes the list as a JSON array (measured on a scratch instance)."""
    langs = subtitle_langs(settings)
    mpv.command("set_property", "slang", langs)
    log.info("player.slang", "set", result="PASS", slang=langs, variant=setting_variant(settings),
             second_variant=setting_variant(settings, "subtitleFallbackLanguage"),
             reason="the viewer's language, then the second language" if fallback_language(settings) not in
             (None, settings.get("subtitleLanguage")) else
             "the viewer's language only (the second language is off or is the viewer's)")
    return langs


def apply_subtitle_font(mpv, log: Log, settings: dict) -> str:
    """Set the running player's `sub-font` for this play, so a changed setting needs no restart."""
    wanted = subtitle_font(settings)
    use = wanted if font_installed(wanted) else MPV_DEFAULT_SUB_FONT
    mpv.command("set_property", "sub-font", use)
    log.info("player.sub_font", "set", result="PASS" if use == wanted else "FALLBACK", wanted=wanted, using=use,
             language=settings.get("subtitleLanguage"), typed=bool(str(settings.get("subtitleFont") or "").strip()),
             reason="follows the subtitle language" if not str(settings.get("subtitleFont") or "").strip()
             else "the family typed in the settings")
    return use
