"""A Prowlarr or Jackett running on this computer, for the Accounts sheet's offer.

When the sheet's Torznab list is empty it runs `torznab find-local`, which asks each product's default port on
loopback one anonymous question and offers the address it answered on. The user still pastes the key, and the
normal add + check path (`torznab_accounts.add`) does everything else; nothing is saved here.

What is asked, and why it is safe (verified 2026-09-30, read-only and from source):
- Prowlarr: `GET /ping` on 9696 answers 200 `{"status":"OK"}` without a key (`PingController`, `[AllowAnonymous]`).
- Jackett: `GET /health` on 9117 answers the same (`HealthcheckController`, `[AllowAnonymous]`, even with an admin
  password set).
- Only `127.0.0.1`, from the constants below; no host, port or path comes from input. The ports can be swapped by a
  test through `find(targets=...)`, never from the command line.
- `http.client` straight, not `Http`/`urllib`: it sends only the headers given (no key, no cookie, no auth header),
  never follows a redirect (a found server answers 200 directly) and never reads `http_proxy`, so the question
  cannot leave the computer.
- A server counts as found only on a 200 whose JSON body is exactly `{"status": "OK"}`. The body is read to at most
  `MAX_BODY` bytes and never logged. Both ports are asked side by side and the answer comes within `TIMEOUT_S` +
  `DEADLINE_PAD_S` (1.25 s), whatever a port does.
- One structured line per port: port, path, result (`found`, `refused`, `no_answer`, `wrong_body`), ms.
"""
from __future__ import annotations

import http.client
import json
import threading
import time

from . import say

COMPONENT = "accounts.torznab"
HOST = "127.0.0.1"
TIMEOUT_S = 1.0              # per port (socket timeout); both ports run at once
DEADLINE_PAD_S = 0.25        # `find` returns within TIMEOUT_S + this, whatever a port does (a stuck port is left)
MAX_BODY = 1024              # `{"status":"OK"}` is 15 bytes; anything past this is not that answer
OK_BODY = {"status": "OK"}


class Target:
    """One product's default: where it listens, the anonymous path it answers on, and the address to pre-fill
    (`{origin}` is `http://127.0.0.1:{port}`)."""

    def __init__(self, product: str, port: int, path: str, address: str):
        self.product, self.port, self.path, self.address = product, int(port), path, address

    def at(self, port: int) -> "Target":
        return Target(self.product, port, self.path, self.address)

    @property
    def hostport(self) -> str:
        return f"{HOST}:{self.port}"

    @property
    def url(self) -> str:
        return self.address.format(origin=f"http://{self.hostport}")


TARGETS = (
    Target("Prowlarr", 9696, "/ping", "{origin}"),
    Target("Jackett", 9117, "/health", "{origin}/api/v2.0/indexers/all/results/torznab"),
)


def _ask(target: Target, timeout: float) -> tuple[str, int | None]:
    """(result, HTTP status or None) for one port. Only the request line, `Host`, `Accept` and
    `Accept-Encoding: identity` go out."""
    conn = http.client.HTTPConnection(HOST, target.port, timeout=timeout)
    try:
        conn.request("GET", target.path, headers={"Accept": "application/json"})
        resp = conn.getresponse()
        body = resp.read(MAX_BODY + 1)
        if resp.status != 200 or len(body) > MAX_BODY:
            return "wrong_body", resp.status
        try:
            ok = json.loads(body.decode("utf-8")) == OK_BODY
        except (UnicodeDecodeError, ValueError):
            ok = False
        return ("found" if ok else "wrong_body"), resp.status
    except ConnectionRefusedError:
        return "refused", None
    except (OSError, http.client.HTTPException):
        # A timeout, a reset, or an answer that is not HTTP: nothing usable answered on this port.
        return "no_answer", None
    finally:
        conn.close()


def find(log, targets=None, timeout: float | None = None) -> dict:
    """Ask every target (`TARGETS` by default) at once and return `{"found": [...], "asked": [...]}`: each found
    server as the sheet offers it (product, address, hostport, words), and each port's result. A port still busy
    when the time is up is `no_answer`; its thread is a daemon and is left behind."""
    targets = TARGETS if targets is None else targets
    timeout = TIMEOUT_S if timeout is None else timeout
    results: dict[int, tuple[str, int | None, float]] = {}
    lock = threading.Lock()

    def run(i: int, target: Target) -> None:
        t0 = time.monotonic()
        result, status = _ask(target, timeout)
        with lock:
            results.setdefault(i, (result, status, (time.monotonic() - t0) * 1000))

    t0 = time.monotonic()
    threads = [threading.Thread(target=run, args=(i, t), daemon=True, name=f"find-local-{t.port}")
               for i, t in enumerate(targets)]
    for th in threads:
        th.start()
    deadline = t0 + timeout + DEADLINE_PAD_S
    for th in threads:
        th.join(max(0.0, deadline - time.monotonic()))
    found, asked = [], []
    with lock:
        for i, target in enumerate(targets):
            result, status, ms = results.setdefault(i, ("no_answer", None, (time.monotonic() - t0) * 1000))
            log.info(COMPONENT, "find_local", result=result, ms=ms, product=target.product, port=target.port,
                     path=target.path, status=status,
                     suggest="" if result in ("found", "refused") else
                     f"something on {target.hostport} is not {target.product}, or did not answer in {timeout:g} s")
            asked.append({"product": target.product, "port": target.port, "path": target.path, "result": result})
            if result == "found":
                found.append({"product": target.product, "address": target.url, "hostport": target.hostport,
                              "words": say.torznab_found(target.product, target.hostport)})
    return {"found": found, "asked": asked}
