"""The plugin's own session-bus client, in the standard library: one method call per connection, nothing in argv.

It follows the D-Bus specification (https://dbus.freedesktop.org/doc/dbus-specification.html, read 2026-10-31), by
section:

- **Server Addresses**: `transport:key=value,…`, each value %-escaped bytes (`%` and two hex digits); a list is
  `;`-separated and tried in order; `guid=` is not needed to connect. **Unix Domain Sockets**: `path=` (a file) and
  `abstract=` (Linux's abstract namespace, a leading NUL) are connectable; `tmpdir=`, `dir=` and `runtime=` only
  listen. Any other transport (`tcp:`, `nonce-tcp:`, `unixexec:`, `launchd:`, `systemd:`, `autolaunch:`) is skipped
  and named in the error: the session bus here is a unix socket.
- **Well-known Message Bus Instances**: the session bus's address is `DBUS_SESSION_BUS_ADDRESS`. Unset, or with no
  connectable entry, the plugin tries `unix:path=$XDG_RUNTIME_DIR/bus`, where systemd's user bus listens (a click run
  by the shell, a watcher started without the variable); neither: no bus, and no guess at `/run/user/<uid>`.
- **Authentication Protocol**: first a single NUL byte ("Special credentials-passing nul byte"), then
  `AUTH EXTERNAL <the uid's ASCII decimal digits, hex-encoded>\\r\\n` (the "EXTERNAL" mechanism, Figure 1: uid 1000 is
  `31303030`). `OK <guid>` accepts, `REJECTED <mechanisms>` refuses; then `BEGIN\\r\\n`, which has no reply.
- **Message Format**: the header is `yyyyuua(yv)`: the byte order (`l`, little-endian), the type (1 METHOD_CALL,
  2 METHOD_RETURN, 3 ERROR, 4 SIGNAL), the flags, the major protocol version 1, the body's length, a non-zero serial,
  the header fields; it is padded to 8 bytes, so the body starts 8-aligned. A reply in either byte order is read.
- **Header Fields**: PATH 1 (`o`), INTERFACE 2 (`s`), MEMBER 3 (`s`), ERROR_NAME 4 (`s`), REPLY_SERIAL 5 (`u`),
  DESTINATION 6 (`s`), SENDER 7 (`s`), SIGNATURE 8 (`g`); a code not known is read past.
- **Marshaling (Wire Format)**: every value aligned to its size counted from the message's first byte, the padding
  NUL bytes; STRING and OBJECT_PATH a UINT32 length in bytes of UTF-8, the text, a NUL; SIGNATURE the same with a
  BYTE length; ARRAY a UINT32 length of its element data, which leaves out the padding to the element's alignment
  (present even when the array is empty); STRUCT and DICT_ENTRY 8-aligned; VARIANT its SIGNATURE, then the value
  aligned to its own type.
- **Message Bus Specification**, `org.freedesktop.DBus.Hello`: the first call on a bus connection, or the bus
  disconnects it. Signals may arrive before a reply (the bus's `NameAcquired`), so the reply to a call is the
  METHOD_RETURN or ERROR whose REPLY_SERIAL is the call's serial; anything else is read past.

One deadline (`TIMEOUT_S`, or the caller's) bounds the whole exchange: the connect, the auth and every read. Every
failure is a `BusError` naming its stage (`address`, `connect`, `auth`, `hello`, `call`, `reply`), its error, what
to check, and which bus it was: the socket connected to, where its address came from and the entries skipped (each
known once it is); the caller logs them. The NUL byte carries no `SCM_CREDENTIALS`: Linux's bus reads the peer's
credentials from the socket itself (`SO_PEERCRED`).
"""
from __future__ import annotations

import os
import re
import socket
import struct
import time

TIMEOUT_S = 15.0
# A message larger than this is refused: a reply here is a few dozen bytes (the specification's own cap is 128 MiB).
MAX_MESSAGE = 1 << 20
BUS = ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus")
METHOD_CALL, METHOD_RETURN, ERROR, SIGNAL = 1, 2, 3, 4
PATH, INTERFACE, MEMBER, ERROR_NAME, REPLY_SERIAL, DESTINATION, SENDER, SIGNATURE = range(1, 9)
# The alignment of each type code (Marshaling: "Summary of D-Bus marshalling").
ALIGN = {"y": 1, "b": 4, "n": 2, "q": 2, "i": 4, "u": 4, "x": 8, "t": 8, "d": 8, "h": 4, "s": 4, "o": 4, "g": 1,
         "v": 1, "a": 4, "(": 8, "{": 8}
FIXED = {"y": "B", "b": "I", "n": "h", "q": "H", "i": "i", "u": "I", "x": "q", "t": "Q", "d": "d", "h": "I"}
SUGGEST = {
    "address": "no session bus address: run the plugin from the desktop session (it reads DBUS_SESSION_BUS_ADDRESS, "
               "else XDG_RUNTIME_DIR/bus)",
    "connect": "the session bus does not answer at its address: check that the desktop session's D-Bus service is "
               "running",
    "auth": "the session bus refused this user (AUTH EXTERNAL): the plugin must run as the desktop session's user",
    "hello": "the session bus did not take the connection (Hello): check that the desktop session's D-Bus service "
             "is running",
    "call": "the bus or the service answered the call with an error: see error",
    "reply": "the answer did not come, or could not be read: check the bus and the service it names",
}
# Seam: the uid the EXTERNAL line names (a test names another to be refused).
_uid = os.getuid


class BusError(Exception):
    """A failed exchange: `stage` (where), the message (what), `suggest` (what to check), `name` (a D-Bus error's
    name, when the bus or the service answered with one), and which bus: `socket` (the one connected to, None before
    a connect), `source` (where the address came from, as `call` returns it) and `skipped` (the address entries passed
    over, with why)."""

    def __init__(self, stage: str, message: str, name: str = "", *, skipped: list[str] | None = None):
        super().__init__(message)
        self.stage, self.name, self.suggest = stage, name, SUGGEST.get(stage, "")
        self.socket, self.source, self.skipped = None, None, list(skipped or [])


def _unescape(value: str) -> bytes:
    raw, out, i = value.encode("utf-8"), bytearray(), 0
    while i < len(raw):
        if raw[i:i + 1] == b"%":
            pair = raw[i + 1:i + 3]
            if not re.fullmatch(rb"[0-9A-Fa-f]{2}", pair):
                raise ValueError(f"a % not followed by two hex digits in {value!r}")
            out.append(int(pair, 16))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return bytes(out)


def candidates(address: str) -> tuple[list[tuple[str, bytes]], list[str]]:
    """The connectable sockets of an address list in order, each `("path"|"abstract", name)`, and why each other
    entry was skipped."""
    found, skipped = [], []
    for entry in (a for a in address.split(";") if a):
        transport, colon, rest = entry.partition(":")
        try:
            if not colon:
                raise ValueError("no transport")
            pairs = [kv.split("=", 1) for kv in rest.split(",") if kv]
            if any(len(kv) != 2 for kv in pairs):
                raise ValueError("a key with no value")
            values = {k: _unescape(v) for k, v in pairs}
        except ValueError as exc:
            skipped.append(f"{entry!r}: {exc}")
            continue
        if transport != "unix":
            skipped.append(f"{transport}: a transport the plugin does not connect to (unix only)")
        elif "path" in values:
            found.append(("path", values["path"]))
        elif "abstract" in values:
            found.append(("abstract", values["abstract"]))
        else:
            skipped.append(f"{entry!r}: unix with neither path= nor abstract= (tmpdir, dir and runtime only listen)")
    return found, skipped


def session_sockets(address: str | None = None) -> tuple[list[tuple[str, bytes]], dict]:
    """The session bus's sockets to try, and where they came from: `address` (a test's), else
    `DBUS_SESSION_BUS_ADDRESS`, else `$XDG_RUNTIME_DIR/bus`. `BusError("address")` when none is known."""
    given = address if address is not None else os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    found, skipped = candidates(given) if given else ([], [])
    source = "given" if address is not None else "DBUS_SESSION_BUS_ADDRESS"
    if not found and address is None:
        runtime = os.environ.get("XDG_RUNTIME_DIR", "")
        if runtime:
            found, source = [("path", os.fsencode(os.path.join(runtime, "bus")))], "XDG_RUNTIME_DIR"
    if not found:
        raise BusError("address", "no connectable session bus address"
                                  + (f" ({'; '.join(skipped)})" if skipped else
                                     ": DBUS_SESSION_BUS_ADDRESS and XDG_RUNTIME_DIR are not set"), skipped=skipped)
    return found, {"source": source, "skipped": skipped}


def split(signature: str) -> list[str]:
    """A signature's complete types, in order ("Valid Signatures")."""
    out, i = [], 0
    while i < len(signature):
        j = i
        while signature[j] == "a":
            j += 1
        if signature[j] in "({":
            depth, close = 0, {"(": ")", "{": "}"}[signature[j]]
            for k in range(j, len(signature)):
                depth += signature[k] == signature[j]
                depth -= signature[k] == close
                if depth == 0:
                    j = k
                    break
            else:
                raise ValueError(f"an unclosed container in {signature!r}")
        out.append(signature[i:j + 1])
        i = j + 1
    return out


class Writer:
    """Marshals values into a block whose first byte is 8-aligned in the message (the header, or the body after it)."""

    def __init__(self):
        self.buf = bytearray()

    def pad(self, n: int) -> None:
        self.buf += b"\0" * (-len(self.buf) % n)

    def put(self, sig: str, value) -> None:
        code = sig[0]
        self.pad(ALIGN[code])
        if code in FIXED:
            self.buf += struct.pack("<" + FIXED[code], value)
        elif code in "so":
            data = value.encode("utf-8")
            self.buf += struct.pack("<I", len(data)) + data + b"\0"
        elif code == "g":
            data = value.encode("ascii")
            self.buf += struct.pack("<B", len(data)) + data + b"\0"
        elif code == "v":
            inner, val = value
            self.put("g", inner)
            self.put(inner, val)
        elif code == "a":
            at = len(self.buf)
            self.buf += b"\0\0\0\0"
            elem = sig[1:]
            self.pad(ALIGN[elem[0]])
            start = len(self.buf)
            for item in (value.items() if elem[0] == "{" else value):
                self.put(elem, item)
            struct.pack_into("<I", self.buf, at, len(self.buf) - start)
        elif code in "({":
            for part, val in zip(split(sig[1:-1]), value):
                self.put(part, val)
        else:
            raise ValueError(f"type {code!r} is not marshalled here")


class Reader:
    """Reads values from a block whose first byte is 8-aligned in the message, in its byte order (`<` or `>`)."""

    def __init__(self, data: bytes, order: str, pos: int = 0):
        self.data, self.order, self.pos = data, order, pos

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ValueError("the message ends inside a value")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def get(self, sig: str):
        code = sig[0]
        self.take(-self.pos % ALIGN[code])
        if code in FIXED:
            fmt = self.order + FIXED[code]
            (value,) = struct.unpack(fmt, self.take(struct.calcsize(fmt)))
            return bool(value) if code == "b" else value
        if code in "sog":
            (n,) = struct.unpack(self.order + ("B" if code == "g" else "I"), self.take(1 if code == "g" else 4))
            text = self.take(n).decode("utf-8")
            if self.take(1) != b"\0":
                raise ValueError("a string not ended by a NUL")
            return text
        if code == "v":
            inner = self.get("g")
            return inner, self.get(inner)
        if code == "a":
            (n,) = struct.unpack(self.order + "I", self.take(4))
            elem = sig[1:]
            self.take(-self.pos % ALIGN[elem[0]])
            end, items = self.pos + n, []
            if end > len(self.data):
                raise ValueError("an array longer than the message")
            while self.pos < end:
                items.append(self.get(elem))
            return dict(items) if elem[0] == "{" else items
        if code in "({":
            return tuple(self.get(part) for part in split(sig[1:-1]))
        raise ValueError(f"type {code!r} is not read here")


def message(serial: int, mtype: int, fields: list[tuple[int, str, object]], signature: str = "", args=(),
            flags: int = 0) -> bytes:
    """One message, little-endian: the header (`yyyyuua(yv)`, padded to 8), then the body."""
    body = Writer()
    for sig, value in zip(split(signature), args):
        body.put(sig, value)
    fields = list(fields) + ([(SIGNATURE, "g", signature)] if signature else [])
    head = Writer()
    for sig, value in (("y", ord("l")), ("y", mtype), ("y", flags), ("y", 1), ("u", len(body.buf)), ("u", serial)):
        head.put(sig, value)
    head.put("a(yv)", [(code, (sig, value)) for code, sig, value in fields])
    head.pad(8)
    return bytes(head.buf + body.buf)


def parse(data: bytes) -> dict:
    """One whole message: `{type, flags, serial, fields, body}`."""
    order = {b"l": "<", b"B": ">"}.get(data[:1])
    if order is None:
        raise ValueError(f"an unknown byte order {data[:1]!r}")
    mtype, flags, version = data[1], data[2], data[3]
    if version != 1:
        raise ValueError(f"protocol version {version}, not 1")
    body_len, serial = struct.unpack(order + "II", data[4:12])
    head = Reader(data, order, 12)
    fields = dict(head.get("a(yv)"))
    head.take(-head.pos % 8)
    sig = fields.get(SIGNATURE, ("g", ""))[1]
    body = Reader(data[head.pos:head.pos + body_len], order)
    return {"type": mtype, "flags": flags, "serial": serial, "fields": {k: v[1] for k, v in fields.items()},
            "body": [body.get(t) for t in split(sig)]}


class Connection:
    """One authenticated connection to a bus, under one deadline (monotonic seconds)."""

    def __init__(self, sock: socket.socket, deadline: float, timeout: float):
        self.sock, self.deadline, self.timeout = sock, deadline, timeout
        self.buf, self.stage, self.serial = b"", "auth", 0
        self.passed = 0                     # messages read past while waiting for a reply (signals, others' replies)

    def _left(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise BusError(self.stage, f"no answer within {self.timeout:g} s")
        return left

    def send(self, data: bytes) -> None:
        self.sock.settimeout(self._left())
        self.sock.sendall(data)

    def _more(self) -> None:
        self.sock.settimeout(self._left())
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            raise BusError(self.stage, f"no answer within {self.timeout:g} s") from None
        if not chunk:
            raise BusError(self.stage, "the bus closed the connection")
        self.buf += chunk

    def line(self) -> bytes:
        while b"\r\n" not in self.buf:
            if len(self.buf) > 4096:
                raise BusError(self.stage, "an authentication line longer than 4096 bytes")
            self._more()
        out, _, self.buf = self.buf.partition(b"\r\n")
        return out

    def exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._more()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def auth(self) -> str:
        """The NUL byte, `AUTH EXTERNAL <uid>`, `OK <guid>`, `BEGIN`. The bus's guid."""
        self.stage = "auth"
        identity = str(_uid()).encode("ascii").hex()
        self.send(b"\0AUTH EXTERNAL " + identity.encode("ascii") + b"\r\n")
        answer = self.line()
        if not answer.startswith(b"OK "):
            raise BusError("auth", f"AUTH EXTERNAL {identity} (uid {_uid()}) answered "
                                   f"{answer[:200].decode('ascii', 'replace')!r}")
        self.send(b"BEGIN\r\n")
        return answer[3:].decode("ascii", "replace")

    def read(self) -> dict:
        fixed = self.exact(16)
        order = {b"l": "<", b"B": ">"}.get(fixed[:1])
        if order is None:
            raise ValueError(f"an unknown byte order {fixed[:1]!r}")
        body_len, _, fields_len = struct.unpack(order + "III", fixed[4:16])
        total = 16 + fields_len + (-(16 + fields_len) % 8) + body_len
        if total > MAX_MESSAGE:
            raise BusError(self.stage, f"a message of {total} bytes, over the {MAX_MESSAGE} this client reads")
        return parse(fixed + self.exact(total - 16))

    def call(self, stage: str, destination: str, path: str, interface: str, member: str, signature: str = "",
             args=()) -> list:
        """One method call and its reply's values; an ERROR reply is a `BusError` with its name and message."""
        self.stage = stage
        self.serial += 1
        serial = self.serial
        self.send(message(serial, METHOD_CALL, [(PATH, "o", path), (INTERFACE, "s", interface),
                                                (MEMBER, "s", member), (DESTINATION, "s", destination)],
                          signature, args))
        while True:
            reply = self.read()
            if reply["type"] not in (METHOD_RETURN, ERROR) or reply["fields"].get(REPLY_SERIAL) != serial:
                self.passed += 1
                continue
            if reply["type"] == ERROR:
                name = str(reply["fields"].get(ERROR_NAME) or "")
                text = reply["body"][0] if reply["body"] and isinstance(reply["body"][0], str) else ""
                raise BusError(stage, f"{name}: {text}" if text else name, name=name)
            return reply["body"]


def call(destination: str, path: str, interface: str, member: str, signature: str = "", args=(), *,
         address: str | None = None, timeout: float | None = None) -> dict:
    """Connect to the session bus (`address` for a test's), authenticate, `Hello`, one call, close.
    `{values, unique_name, socket, source, skipped, passed, ms}`; any failure a `BusError`."""
    timeout = TIMEOUT_S if timeout is None else timeout
    t0 = time.monotonic()
    deadline = t0 + timeout
    found, where = session_sockets(address)
    sock, errors, used = None, [], None

    def failed(exc: BusError) -> BusError:
        exc.socket, exc.source, exc.skipped = used, where["source"], where["skipped"]
        return exc
    for kind, name in found:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(max(deadline - time.monotonic(), 0.001))
            sock.connect(name if kind == "path" else b"\0" + name)
            used = f"{kind}={os.fsdecode(name)}"
            break
        except OSError as exc:
            sock.close()
            sock = None
            errors.append(f"{kind}={os.fsdecode(name)}: {type(exc).__name__}: {exc}")
    if sock is None:
        raise failed(BusError("connect", "; ".join(errors)))
    conn = Connection(sock, deadline, timeout)
    try:
        conn.auth()
        (unique,) = conn.call("hello", *BUS, "Hello")
        values = conn.call("call", destination, path, interface, member, signature, args)
    except BusError as exc:
        raise failed(exc)
    except (ValueError, struct.error, UnicodeError, KeyError, IndexError, TypeError) as exc:
        raise failed(BusError("reply" if conn.stage == "call" else conn.stage,
                              f"a message that could not be read: {type(exc).__name__}: {exc}")) from None
    except OSError as exc:
        raise failed(BusError(conn.stage, f"{type(exc).__name__}: {exc}")) from None
    finally:
        sock.close()
    return {"values": values, "unique_name": unique, "socket": used, **where, "passed": conn.passed,
            "ms": (time.monotonic() - t0) * 1000}
