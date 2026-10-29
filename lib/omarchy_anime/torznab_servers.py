"""The Torznab servers file: its one owner (the Torznab search reads it, the Accounts sheet writes it).

`~/.config/omaanime/torznab-servers.json` (under `$XDG_CONFIG_HOME` if set), next to the TorBox token:

    {"version": 1, "servers": [{"url": "...", "key": "...", "name": "..."}]}

`url` is the address as the viewer pasted it; `key` its API key (`""` for a feed without one); `name` optional.
Unknown fields are ignored, so the add-on can add its own; a change that would make this reader misread a server
bumps `version`. The file holds secrets, so it is read only when it is a regular, private (mode 600) file,
and written only through `write` (atomic, mode 600). Turning a server into the feeds it searches is not this
module's business: that is `torznab.source_of`/`feeds_of`, and a Prowlarr's expansion happens in the add-on's
call, remembered in the add-on's cache, not in this file.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import core

FILE = "torznab-servers.json"
LOCK = ".torznab-servers.lock"        # held around every read-modify-write (`locked`)
KNOWN = ("url", "key", "name")        # an entry's fields this version reads; any other is kept as it is
VERSION = 1
# A key shorter than this is refused: the log scrubber masks known secrets from 8 characters up (a shorter
# needle would mask ordinary text), so a short key could reach a log line. Prowlarr and Jackett keys are 32.
MIN_KEY_LEN = 8


@dataclass(frozen=True)
class Server:
    """One entry of the file, as pasted."""
    url: str
    key: str = ""
    name: str = ""


@dataclass
class ServersFile:
    """What the file gave: `servers` usable, `problem` why the whole file is unusable ("" when fine or absent),
    `skipped` one `{server, reason}` per entry left out (numbered from 1, in file order). `extras`
    holds, per server, the entry's fields this version does not read, and `top` the file's own, so a rewrite keeps
    them."""
    servers: list = field(default_factory=list)
    problem: str = ""
    skipped: list = field(default_factory=list)
    extras: list = field(default_factory=list)
    top: dict = field(default_factory=dict)


def path() -> Path:
    return core.CONFIG_DIR / FILE


def _regular(p: Path) -> tuple[bool, str]:
    """(exists, problem). Only a regular file is read: not a symlink (it would read whatever it points at),
    a FIFO (reading one blocks the command), a folder or a device. `lstat`, so nothing is followed or opened."""
    try:
        st = p.lstat()
    except FileNotFoundError:
        return False, ""
    except OSError as exc:
        return True, f"{p} could not be read ({type(exc).__name__}); check its permissions"
    if not stat.S_ISREG(st.st_mode):
        return True, f"{p} is not a regular file; it must be a plain file written by the plugin (remove it)"
    mode = stat.S_IMODE(st.st_mode)
    if not core.is_private_mode(mode):
        return True, (f"{p} has mode {mode:o}; it holds API keys, so it is not read until it is private: "
                      f"chmod 600 {p}")
    return True, ""


def read() -> ServersFile:
    """The servers file, for one command. No file, or an empty list: no server and no problem (the registry is
    then exactly as without Torznab). Refused whole (`problem`): not a regular file, readable by others,
    unreadable, not JSON, not `{"servers": [...]}`, another `version`. An entry that is not an object with a
    text `url` and `key`, or whose key is shorter than `MIN_KEY_LEN`, is skipped with its reason."""
    p = path()
    exists, problem = _regular(p)
    if not exists or problem:
        return ServersFile(problem=problem)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return ServersFile(problem=f"{p} is not valid JSON ({type(exc).__name__}); fix or remove it")
    if not isinstance(data, dict) or not isinstance(data.get("servers", []), list):
        return ServersFile(problem=f"{p} must be an object with a \"servers\" list")
    version = data.get("version", VERSION)
    if version != VERSION:
        return ServersFile(problem=f"{p} is version {version!r}; this plugin reads version {VERSION}")
    out = ServersFile(top={k: v for k, v in data.items() if k not in ("version", "servers")})
    for n, entry in enumerate(data.get("servers") or [], 1):
        url = entry.get("url") if isinstance(entry, dict) else None
        key = entry.get("key", "") if isinstance(entry, dict) else None
        if not isinstance(url, str) or not url.strip() or not isinstance(key, str):
            out.skipped.append({"server": n, "reason": "not an object with a text url and key"})
            continue
        key = key.strip()
        if key and len(key) < MIN_KEY_LEN:
            out.skipped.append({"server": n, "reason": f"its key is shorter than {MIN_KEY_LEN} characters, too short "
                                                       f"to keep out of the log; Prowlarr and Jackett keys are 32"})
            continue
        out.servers.append(Server(url=url.strip(), key=key, name=str(entry.get("name") or "")))
        out.extras.append({k: v for k, v in entry.items() if k not in KNOWN})
    return out


def keys() -> list[str]:
    """Every key in the file, and in a temp copy an interrupted `write` left behind, whatever the file's mode
    or shape, for the log scrubber alone (`core.known_secrets`): a file the add-on refuses to use still holds
    keys that must never be printed. Regular files only (a FIFO would block every command)."""
    out: list[str] = []
    try:
        candidates = [path()] + sorted(core.CONFIG_DIR.glob(f".{FILE}.tmp.*"))
    except OSError:
        return out
    for p in candidates:
        try:
            if not stat.S_ISREG(p.lstat().st_mode):
                continue
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        servers = data.get("servers") if isinstance(data, dict) else None
        for s in servers if isinstance(servers, list) else []:
            if isinstance(s, dict) and isinstance(s.get("key"), str) and s["key"].strip():
                out.append(s["key"].strip())
    return out


@contextlib.contextmanager
def locked():
    """Hold the servers file's lock (`flock` on a mode-600 sidecar beside it) for one read-modify-write, so two
    writers (the sheet and a terminal, two terminals) cannot each write the list they read before the other's
    change. Blocks until the other writer is done; never held across a network call."""
    folder = core.CONFIG_DIR
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(folder / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write(servers: list[Server], extras: list | None = None, top: dict | None = None) -> Path:
    """The one writer (the Accounts sheet): the whole list, atomically at mode 600 (`core.write_private`), and
    the scrubber told at once (`core.forget_secrets`, inside `write_private`'s caller contract). `extras[i]` (the
    fields of entry i this version does not read) and `top` (the file's own) are written back as they were read."""
    extras = list(extras or [])
    entries = [{**(extras[i] if i < len(extras) and isinstance(extras[i], dict) else {}), **asdict(s)}
               for i, s in enumerate(servers)]
    body = json.dumps({**dict(top or {}), "version": VERSION, "servers": entries}, ensure_ascii=False, indent=1)
    written = core.write_private(path(), body + "\n")
    core.forget_secrets()
    return written
