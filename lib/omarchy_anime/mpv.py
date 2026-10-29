"""Minimal mpv JSON IPC client and launcher glue."""
from __future__ import annotations

import json
import math
import os
import random
import re
import socket
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .core import Log, StageError, MPV_SOCKET, PLAYER_LAUNCHER, load_settings

# ----------------------------------------------------------------------------- mpv

class PlayerClosed(StageError):
    """The player went away while the plugin talked to it: a write to its socket broke (`BrokenPipeError`, seen
    2026-10-22 when a player restart killed it 11 s into a play, trace `ui-play-1792707494-393`), the connection was
    reset, or a read found the socket closed. A `StageError`, so a caller that reads mpv's failures reads this one
    too; `cli.do_play` says it in plain words ("The player closed while starting episode 9. Play it again.")."""


# The errors a socket whose other end has gone raises on a write or a read.
CLOSED_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class ForeignPeer(StageError):
    """The process answering on the socket path runs as another user: nothing is sent to it (a play's `loadfile`
    carries the TorBox key), and no player is launched over it (the launcher's `rm -f` cannot remove another user's
    socket in a shared folder)."""


def tls_refusal(msg: dict) -> str | None:
    """ffmpeg's refusal of a server's certificate in a `log-message` event, else None.

    mpv's `end-file` says only `loading failed` for it, the same as for a refused connection or a 404; ffmpeg's error
    line is the one place the certificate is named. Measured on mpv 0.41 with ffmpeg built on GnuTLS (2026-10-29):
    `tls: Peer certificate failed verification` (self-signed) and `tls: The certificate's owner does not match
    hostname <host>` (another name's). Only this line is ever kept: the `stream` line beside it, `Failed to open
    https://…?token=…`, carries the key."""
    text = str(msg.get("text") or "").strip()
    if (msg.get("prefix") == "ffmpeg" and text.startswith("tls:") and "certificate" in text.lower()
            and "?" not in text and "token" not in text.lower()):
        return text[:200]
    return None


def peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    """`(pid, uid, gid)` of the process listening on a connected Unix socket: `SO_PEERCRED`, which unix(7) says holds
    the peer's credentials "at the time of the call to listen(2)", so a hung mpv that never accepts still answers.
    Module level so a test can stand in another uid."""
    return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))


# Where `_owner_pid` scans for the player when the socket names no peer.
PROC = Path("/proc")


class Mpv:
    """Minimal JSON IPC client. Protocol verified live on mpv 0.41 (2026-09-14)."""

    def __init__(self, log: Log, path: Path | None = None):
        # Resolved at call time, not bound as a default: the tests point MPV_SOCKET at a temp socket.
        self.log, self.path, self._id = log, path or MPV_SOCKET, 0
        self.sock: socket.socket | None = None
        self._buf = b""
        self.peer_pid: int | None = None        # the listener's pid, from `SO_PEERCRED` at connect

    def ensure_running(self) -> None:
        """Connect to the player, or start it. An mpv whose core no longer answers IPC (its video thread
        stuck on a GPU fence) is killed and replaced, so one hung player never takes the plugin down with it."""
        if self.path.exists() and self._try_connect():
            if self.healthy(timeout=3.0):
                return
            pid = self._owner_pid()
            self.log.error("mpv.watchdog", "unresponsive", result="FAIL", pid=pid, socket=str(self.path),
                           suggest="mpv stopped answering IPC; killing it and starting a fresh player")
            self._close()
            if pid:
                self._kill(pid)
            self.path.unlink(missing_ok=True)
        self._launch()
        if not self._try_connect(retries=30):
            raise StageError("mpv.launch", "mpv did not expose its IPC socket", f"run {PLAYER_LAUNCHER} by hand")

    def _launch(self) -> None:
        if not PLAYER_LAUNCHER.exists():
            raise StageError("mpv.launch", f"launcher missing: {PLAYER_LAUNCHER}")
        t0 = time.monotonic()
        settings = load_settings()
        from . import player as playermod          # local: player imports this module's neighbours
        langs = playermod.subtitle_langs(settings)
        env = dict(os.environ, OMARCHY_ANIME_SUB_LANG=langs[0], OMARCHY_ANIME_SUB_SECOND=",".join(langs[1:]),
                   OMARCHY_ANIME_SUB_FONT=playermod.subtitle_font(settings))
        proc = subprocess.run([str(PLAYER_LAUNCHER)], env=env, capture_output=True, text=True, timeout=20)
        self.log.info("mpv.launch", "spawn", result="PASS" if proc.returncode == 0 else "FAIL",
                      ms=(time.monotonic() - t0) * 1000, rc=proc.returncode, out=proc.stdout.strip()[:200],
                      err=proc.stderr.strip()[:200])
        if proc.returncode != 0:
            raise StageError("mpv.launch", f"launcher exited {proc.returncode}: {proc.stderr.strip()[:200]}",
                             f"run {PLAYER_LAUNCHER} by hand")

    def healthy(self, timeout: float = 3.0) -> bool:
        """One cheap round trip. False when the socket accepts but the core never answers."""
        if self.sock is None and not self._try_connect():
            return False
        t0 = time.monotonic()
        try:
            self.command("get_property", "pid", timeout=timeout)
            return True
        except StageError as exc:
            self.log.warn("mpv.watchdog", "ping", result="FAIL", ms=(time.monotonic() - t0) * 1000, error=str(exc))
            return False

    def _close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock, self._buf, self.peer_pid = None, b"", None

    def _owner_pid(self) -> int | None:
        """The process serving our socket (the pid property needs a live IPC): the listener `SO_PEERCRED` named at
        connect, else one of this user's own processes whose program is `mpv` and whose command line holds the socket
        path. Another user's process is never taken: anyone can start an `mpv` naming this path."""
        if self.peer_pid:
            return self.peer_pid
        needle = str(self.path).encode()
        me, uid = os.getpid(), os.getuid()
        for entry in os.listdir(PROC):
            if not entry.isdigit() or int(entry) == me:
                continue
            try:
                if (PROC / entry).stat().st_uid != uid:
                    continue
                cmd = (PROC / entry / "cmdline").read_bytes()
            except OSError:
                continue
            if needle in cmd and b"mpv" in os.path.basename(cmd.split(b"\0", 1)[0]):     # bytes: any name reads
                return int(entry)
        return None

    def _kill(self, pid: int) -> None:
        import signal
        t0 = time.monotonic()
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            self.log.info("mpv.watchdog", "kill", result="PASS", pid=pid, note="already gone")
            return
        except PermissionError:
            self.log.warn("mpv.watchdog", "kill", result="FAIL", pid=pid,
                          suggest="not allowed to signal it: it is not this user's player; it is left alone")
            return
        for _ in range(50):
            if not Path("/proc", str(pid)).exists():
                break
            time.sleep(0.1)
        self.log.info("mpv.watchdog", "kill", result="PASS", pid=pid, ms=(time.monotonic() - t0) * 1000)

    def _try_connect(self, retries: int = 1) -> bool:
        """Connect to the socket path, and keep the connection only when the listener runs as this user
        (`SO_PEERCRED`): a peer of another uid is closed before a byte is sent and raises `ForeignPeer`."""
        for _ in range(retries):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(5)
                s.connect(str(self.path))
            except OSError:
                time.sleep(0.1)
                continue
            try:
                pid, uid, _gid = peer_credentials(s)
            except OSError as exc:
                s.close()
                self.log.error("mpv.peer", "refused", result="FAIL", socket=str(self.path), error=str(exc),
                               suggest="the socket's owner could not be read; nothing was sent to it")
                raise ForeignPeer("mpv.peer", "the player socket's owner could not be read",
                                  "check $XDG_RUNTIME_DIR: it should be your own folder (mode 0700)") from exc
            if uid != os.getuid():
                s.close()
                self.log.error("mpv.peer", "refused", result="FAIL", socket=str(self.path), peer_uid=uid,
                               peer_pid=pid, uid=os.getuid(),
                               suggest="another user answers on the player socket; nothing was sent to it. "
                                       "Check $XDG_RUNTIME_DIR: it should be your own folder (mode 0700)")
                raise ForeignPeer("mpv.peer", f"the player socket {self.path} belongs to another user (uid {uid})",
                                  "check $XDG_RUNTIME_DIR: it should be your own folder (mode 0700)",
                                  peer_uid=uid)
            self.sock, self.peer_pid = s, pid
            return True
        return False

    def command(self, *cmd, timeout: float = 10.0):
        assert self.sock is not None
        self._id += 1
        rid = self._id
        payload = json.dumps({"command": list(cmd), "request_id": rid}) + "\n"
        t0 = time.monotonic()
        try:
            self.sock.sendall(payload.encode())
        except CLOSED_ERRORS as exc:
            raise PlayerClosed("mpv.ipc", f"the player closed ({type(exc).__name__} on {cmd[0]})",
                               error=type(exc).__name__, command=cmd[0]) from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self._read_line(deadline)
            except PlayerClosed as exc:
                exc.data.setdefault("command", cmd[0])        # which command found the player gone, for the log
                raise
            if msg is None:
                # One socket read timed out (5 s slices); only the deadline decides.
                continue
            if msg.get("request_id") == rid:
                self.log.debug("mpv.ipc", "reply", ms=(time.monotonic() - t0) * 1000, command=cmd[0],
                               error=msg.get("error"), data=str(msg.get("data"))[:120])
                if msg.get("error") != "success":
                    raise StageError("mpv.ipc", f"{cmd[0]} failed: {msg.get('error')}")
                return msg.get("data")
        raise StageError("mpv.ipc", f"no reply to {cmd[0]} within {timeout}s")

    def sub_add(self, path: str, title: str, lang: str) -> dict:
        """`sub-add <abs path> select <title> <lang>`, then read the result back.

        Verified live on mpv 0.41 over a headless IPC socket, 2026-09-16: `success` in **27 ms**;
        `current-tracks/sub/lang` = "ara", `/external` = true, `/title` = "Arabic (external)",
        `/codec` = "subrip"; `track-list` gains `{id:1, lang:"ara", external:true, selected:true}`.
        A bad path answers `{"error":"error running command"}` and **leaves the previously selected
        track selected** — a clean failure with no state damage. (`sub-files` stays empty: that is
        `--sub-file`'s list, not `sub-add`'s.)
        """
        t0 = time.monotonic()
        self.command("sub-add", str(path), "select", title, lang)
        out = {"lang": None, "external": None, "title": None, "codec": None}
        for key in list(out):
            try:
                out[key] = self.command("get_property", f"current-tracks/sub/{key}")
            except StageError:
                pass
        out["ms"] = round((time.monotonic() - t0) * 1000, 1)
        self.log.info("subs.load", "sub-add", result="PASS" if out["lang"] else "FAIL", ms=out["ms"],
                      path=str(path), lang_readback=out["lang"], external=out["external"],
                      suggest="" if out["lang"] else "mpv accepted sub-add but reports no selected subtitle track")
        return out

    def set_pause(self, paused: bool) -> bool | None:
        """`set_property pause`, then read it back. Returns what mpv reports, or None.

        The player runs with `--keep-open=yes`, and the installed man page says that
        *"will act like `set pause yes` on EOF, unless the --keep-open-pause=no option is set"*.
        `--reset-on-next-file` then *"Default: Do not reset anything"*, so that pause survives the
        `loadfile` and the next episode opens on a paused first frame. Same mechanism as
        `set_sub_delay` below, one property over.

        Never raises: a play that has already found and opened a stream must not fail because the
        unpause did. A refused write returns None and says so in the log.
        """
        before = None
        try:
            before = self.command("get_property", "pause")
        except StageError:
            pass
        try:
            self.command("set_property", "pause", bool(paused))
            after = self.command("get_property", "pause")
        except StageError as exc:
            self.log.warn("mpv.pause", "set", result="FAIL", before=before, wanted=bool(paused),
                          error=str(exc),
                          suggest="mpv refused the write; the episode may have opened paused — press space")
            return None
        ok = after is not None and bool(after) == bool(paused)
        self.log.emit("info" if ok else "warn", "mpv.pause", "set", result="PASS" if ok else "FAIL",
                      before=before, wanted=bool(paused), after=after,
                      suggest="" if ok else "mpv accepted the write but reports a different pause state")
        return bool(after) if after is not None else None

    def reset_for_new_file(self, keep_paused: bool = False) -> dict:
        """Clear what the file that just ended left set on the player.

        mpv keeps every property across `loadfile` — `--reset-on-next-file` defaults to resetting
        nothing — so this is the explicit list of the ones that carry over and matter:

        - `pause`, set to yes by `--keep-open=yes` at EOF, which made every episode
          after the first open paused;
        - `sub-delay`, so a nudge typed for one episode does not silently mistime the next.

        Neither failure may fail the play, and neither may prevent the other being attempted. `keep_paused`: the file
        opened paused on purpose (ready while nobody watches), and stays so.
        """
        out: dict = {"pause": None, "subDelay": None}
        try:
            out["pause"] = self.set_pause(bool(keep_paused))
        except StageError as exc:
            self.log.warn("mpv.pause", "reset", result="FAIL", error=str(exc))
        try:
            out["subDelay"] = self.set_sub_delay(0.0)
        except StageError as exc:
            self.log.warn("mpv.subdelay", "reset", result="FAIL", error=str(exc),
                          suggest="a stale sub-delay from a previous episode may still be applied")
        return out

    def set_sub_delay(self, seconds: float) -> float:
        """`set_property sub-delay <seconds>`, then read it back. Returns what mpv reports.

        Positive delays the subtitles (`--sub-delay=<sec>`: *"Delays primary subtitles by <sec>
        seconds. Can be negative."*, installed man page).

        This is the graceful-degradation path for a side-load whose offset could not be estimated:
        mpv's own `z`/`Z` move sub-delay 0.1 s per press and only while the mpv window
        holds keyboard focus, which is 388 presses for a 38.74 s error.

        It is also why `do_play` calls this with 0.0 on every load: `--reset-on-next-file` defaults
        to *"Do not reset anything"*, so sub-delay survives `loadfile` and a nudge typed for one
        episode would otherwise mistime the next one silently.
        """
        before = None
        try:
            before = self.command("get_property", "sub-delay")
        except StageError:
            pass
        self.command("set_property", "sub-delay", float(seconds))
        after = self.command("get_property", "sub-delay")
        ok = after is not None and abs(float(after) - float(seconds)) < 0.001
        self.log.emit("info" if ok else "warn", "mpv.subdelay", "set", result="PASS" if ok else "FAIL",
                      before=before, wanted=round(float(seconds), 3), after=after,
                      suggest="" if ok else "mpv accepted the write but reports a different sub-delay")
        return float(after) if after is not None else 0.0

    # ------------------------------------------------------------------ scripts

    def script_message_to(self, target: str, *args) -> bool:
        """`script-message-to <script> <msg> [args…]`. Never raises: the caller is on the play path.

        Measured on mpv 0.41 over an IPC socket: both `script-message` (broadcast) and
        `script-message-to` (targeted) reach a loaded script. A player with no such script answers
        `error running command`, which is a normal outcome here — the panel simply is not loaded.
        """
        try:
            self.command("script-message-to", str(target), *[str(a) for a in args])
            self.log.debug("mpv.script", "message", result="PASS", target=target, args=[str(a) for a in args])
            return True
        except StageError as exc:
            self.log.warn("mpv.script", "message", result="FAIL", target=target, args=[str(a) for a in args],
                          error=str(exc), suggest="the episode panel is not loaded in this player")
            return False

    def ensure_script(self, path: str, key: str) -> bool:
        """Load `path` into a player that is already running, once.

        The guard is `user-data/<key>`, which the script sets on load. The `scripts` property is
        **not** usable for this: it reflects only `--script=` command-line arguments (measured: it
        stayed `[]` on a live player that had a script loaded over IPC). Getting the guard wrong
        costs a second copy of the script that cannot be unloaded without restarting mpv.
        """
        try:
            already = self.command("get_property", f"user-data/{key}")
        except StageError:
            already = None
        if already:
            self.log.debug("mpv.script", "ensure", result="PASS", key=key, loaded=True)
            return True
        try:
            data = self.command("load-script", str(path))
        except StageError as exc:
            self.log.warn("mpv.script", "load", result="FAIL", path=str(path), error=str(exc),
                          suggest="the player keeps working without the episode panel")
            return False
        self.log.info("mpv.script", "load", result="PASS", path=str(path), key=key, data=str(data)[:80])
        return True

    def wait_event(self, name: str, timeout: float, on_tick=None, tick_s: float = 1.0) -> dict | None:
        """Wait for one mpv event.

        `on_tick` is called at least every `tick_s` while waiting. It exists because this is the
        last stretch of a play the user is watching, and a wait that says nothing is what
        made the whole feature look dead. It must not raise; it is on the play path.

        A wait for `file-loaded` ends the error lines `player.load_file` asked for on this connection, whatever ended
        it: the play goes on with this connection, and those lines are not for it.
        """
        try:
            return self._wait_event(name, timeout, on_tick, tick_s)
        finally:
            if name == "file-loaded":
                self.tell("request_log_messages", "no")

    def _wait_event(self, name: str, timeout: float, on_tick, tick_s: float) -> dict | None:
        deadline = time.monotonic() + timeout
        refused = None              # ffmpeg's certificate refusal, from the error lines `player.load_file` asked for
        while time.monotonic() < deadline:
            slice_end = min(deadline, time.monotonic() + tick_s) if on_tick else deadline
            msg = self._read_line(slice_end)
            if msg is None:
                if on_tick:
                    try:
                        on_tick()
                    except Exception as exc:                  # noqa: BLE001 - never fail a play for a status line
                        self.log.warn("mpv.ipc", "tick", result="FAIL", error=str(exc))
                continue          # a quiet read slice is not the deadline
            if msg.get("event") == "log-message":
                refused = tls_refusal(msg) or refused
                continue
            if msg.get("event") == name:
                return msg
            if msg.get("event") == "end-file" and name == "file-loaded":
                # `loadfile ... replace` first ends the file that was playing (reason "stop"; "redirect" for
                # playlists). Only the new file's failure ("error", or an unexpected reason) aborts the wait.
                if msg.get("reason") in ("stop", "redirect"):
                    self.log.debug("mpv.ipc", "end-file", result="PASS", reason=msg.get("reason"),
                                   note="previous file unloaded; still waiting for file-loaded")
                    refused = None          # a line before it belonged to the file it ended, not the one loading
                    continue
                if refused:
                    msg = {**msg, "tls_refused": refused}
                self.log.warn("mpv.ipc", "end-file", result="FAIL", reason=msg.get("reason"),
                              file_error=msg.get("file_error"), **({"tls_refused": refused} if refused else {}))
                return msg
        return None

    def tell(self, *cmd) -> None:
        """Sends `cmd` without waiting for its reply, which `command` skips like any other line (it has no
        `request_id` of ours). For a clean-up that must neither raise nor wait on a player that stopped answering."""
        if self.sock is None:
            return
        try:
            self.sock.sendall((json.dumps({"command": list(cmd)}) + "\n").encode())
        except OSError as exc:
            self.log.debug("mpv.ipc", "tell", result="FAIL", command=cmd[0], error=type(exc).__name__)

    def _read_line(self, deadline: float) -> dict | None:
        assert self.sock is not None
        while b"\n" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(min(remaining, 5))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            except CLOSED_ERRORS as exc:
                raise PlayerClosed("mpv.ipc", f"mpv closed the socket ({type(exc).__name__})",
                                   error=type(exc).__name__) from exc
            if not chunk:
                raise PlayerClosed("mpv.ipc", "mpv closed the socket", error="EOF")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {}


