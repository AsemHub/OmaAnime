"""TorBox sign-in with a device code: the user types a 6-digit code at a short URL, or scans a
QR code, and never has to find and paste an API token.

Every field below is from TorBox's public API docs (the Postman collection behind
api-docs.torbox.app, requests "Start Device Code Authorization Process" and "Get Token From Device
Code Process", read 2026-09-28):

  * `GET /v1/api/user/auth/device/start?app=<name>`, no auth. `data` holds `device_code`, `code`
    (the 6 digits the user types), `verification_url` (for a QR code), `friendly_verification_url`
    (short, for plain text), `interval` and `expires_at` (ISO 8601, UTC). The device code lives 10
    minutes. `app` is shown on the approval page and is alphanumeric only.
  * `POST /v1/api/user/auth/device/token` with the JSON body `{"device_code": …}`, no auth. Success
    is `data.access_token` (+ `token_type: "Bearer"`). Failures are 400 with an `error` code:
    `DEVICE_CODE_NOT_USED` (not approved yet), `ITEM_NOT_FOUND` ("invalid or has expired"),
    `DATABASE_ERROR` ("No API token found for this user"), and `UNKNOWN_ERROR` (no status given).

Not in the docs, and so handled defensively rather than assumed: what a **declined** sign-in
answers (no example exists; an unknown error code naming a denial ends the wait as declined, any
other unknown answer as "TorBox answered unexpectedly", and `ITEM_NOT_FOUND` well before the
deadline is read as "declined or cancelled"), what `interval` means (the example is
`0`, so it is floored at `MIN_INTERVAL_S`), and whether `expires_at` agrees with this machine's
clock (the wait is bounded by `WINDOW_S` from this machine's own start regardless).

The device code redeems the account token for ten minutes, so it is treated like the token: it is
never logged, printed or returned to a caller outside this module's wait.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import time
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from . import core
from .core import Http, Log, StageError
from .providers import header

APP_NAME = "OmaAnime"          # shown on TorBox's approval page; alphanumeric only (docs)
WINDOW_S = 600.0               # "The device_code is only valid for 10 minutes"
MIN_INTERVAL_S = 5.0           # the documented example interval is 0; never hammer the endpoint
MAX_INTERVAL_S = 15.0          # an approved user never waits much longer than this for the pickup
BACKOFF = 1.25                 # per pending answer; a transient failure doubles instead
RETRY_AFTER_MAX_S = 60.0       # a 429's Retry-After is honoured up to this (and never past the deadline)
GONE_EARLY_S = 15.0            # ITEM_NOT_FOUND this long before the deadline: not an expiry

PENDING = "DEVICE_CODE_NOT_USED"
GONE = "ITEM_NOT_FOUND"
NO_TOKEN = "DATABASE_ERROR"
TRANSIENT = ("UNKNOWN_ERROR",)
# No denial answer is documented. An error code naming one of these is read as the user declining;
# any other unknown answer (a Cloudflare 403 page, a new code) is "TorBox answered unexpectedly".
DENIAL_WORDS = ("DENI", "DECLIN", "REJECT", "REFUS", "CANCEL")

COMPONENT = "torbox.device"
AGAIN = "run it again: omarchy-anime auth torbox --device"

# The wait's clock and sleep, looked up at call time so a test can run the ten minutes instantly.
CLOCK: Callable[[], float] = time.monotonic
SLEEP: Callable[[float], None] = time.sleep


def _url(path: str) -> str:
    # Read at call time, so a test's fake server (patched on `core`) is the one called.
    return f"{core.TORBOX_URL}{path}"


def _server_now(answered) -> datetime | None:
    """TorBox's own clock, from the start answer's `Date` header (RFC 9110), or None."""
    try:
        at = parsedate_to_datetime(str(header(answered, "Date") or ""))
    except (TypeError, ValueError, IndexError):
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def _expiry(value, log: Log, server_now: datetime | None = None) -> tuple[float, str]:
    """Seconds left per `expires_at`, bounded to the documented window. The length is taken
    against TorBox's own clock (the answer's `Date`) when it sent one, so a local clock that is off
    does not shorten the wait; the caller then counts it down on the monotonic clock. A missing,
    unreadable or already-past value falls back to the whole window: TorBox's own `ITEM_NOT_FOUND`
    is the real expiry signal, and this bound only decides when the wait stops asking."""
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        left = (at - (server_now or datetime.now(timezone.utc))).total_seconds()
    except (TypeError, ValueError):
        log.warn(COMPONENT, "expiry", result="FAIL", expires_at=str(value)[:40], window_s=WINDOW_S,
                 suggest="TorBox's expires_at was not ISO 8601; waiting the documented 10 minutes")
        return WINDOW_S, "unreadable"
    if left <= 0:
        log.warn(COMPONENT, "expiry", result="FAIL", expires_at=str(value), left_s=round(left, 1),
                 window_s=WINDOW_S, suggest="expires_at is already past here: this clock or TorBox's is off")
        return WINDOW_S, "past"
    return min(left, WINDOW_S), "server date" if server_now else "local clock"


# The hosts of TorBox's own sign-in pages: `friendly_verification_url` (`https://tor.box/link`) and `verification_url`
# (`https://torbox.app/oauth/device?…`) in the Postman collection's example answer (read 2026-09-28). A subdomain
# of torbox.app is TorBox's too. No `start` answer is recorded in the logs to measure against.
PAGE_HOSTS = ("tor.box", "torbox.app")


def page_url(value) -> str:
    """`value` when it is a page the sheet may show, encode and open: `https` on one of `PAGE_HOSTS` (or a subdomain of
    torbox.app), no user part, the default port. Else "": `xdg-open` would open any scheme's handler or a local file."""
    text = str(value or "")
    try:
        parts = urllib.parse.urlsplit(text)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return ""
    ours = host in PAGE_HOSTS or host.endswith(".torbox.app")
    clean = "@" not in parts.netloc and port in (None, 443) and not any(ord(c) < 33 for c in text)
    return text if parts.scheme == "https" and ours and clean else ""


def start(http: Http, log: Log, *, clock: Callable[[], float] | None = None) -> dict:
    """Ask TorBox for a device code. The returned dict carries the device code under
    `_device_code`: it is for `wait_for_token` only, and every caller that prints shows `public()`."""
    clock = clock or CLOCK
    url = _url("/user/auth/device/start") + "?" + urllib.parse.urlencode({"app": APP_NAME})
    t0 = clock()
    status, body, answered = http.request(f"{COMPONENT}.start", "GET", url)
    data = body.get("data") if isinstance(body, dict) else None
    if status != 200 or not isinstance(body, dict) or body.get("success") is False or not isinstance(data, dict):
        err = body.get("error") if isinstance(body, dict) else None
        detail = body.get("detail") if isinstance(body, dict) else str(body)[:200]
        log.error(COMPONENT, "start", result="FAIL", status=status, error=err, detail=detail,
                  suggest="TorBox did not start a device sign-in; try again, or paste a token")
        raise StageError(f"{COMPONENT}.start", f"TorBox could not start a sign-in ({status}): {err or detail}",
                         "try again in a minute, or paste a token: omarchy-anime auth torbox", status=status)
    device_code, code = str(data.get("device_code") or ""), str(data.get("code") or "")
    sent = {"friendly_verification_url": data.get("friendly_verification_url"),
            "verification_url": data.get("verification_url")}
    for key, value in sent.items():
        if value and not page_url(value):
            try:
                origin = core.origin_label(str(value))
            except ValueError:
                origin = "unreadable"
            log.warn(COMPONENT, "url_refused", result="FAIL", field=key, origin=origin,
                     suggest="TorBox's sign-in page is not https on tor.box or torbox.app; it is not shown or opened")
    friendly = page_url(sent["friendly_verification_url"])
    verification = page_url(sent["verification_url"]) or friendly
    if not device_code or not code or not (friendly or verification):
        log.error(COMPONENT, "start", result="FAIL", status=status, fields=sorted(data),
                  suggest="TorBox's start answer is missing device_code, code or a URL; the API may have changed")
        raise StageError(f"{COMPONENT}.start", "TorBox's answer had no code to show",
                         "paste a token instead: omarchy-anime auth torbox")
    left, basis = _expiry(data.get("expires_at"), log, _server_now(answered))
    try:
        api_interval = max(float(data.get("interval") or 0), 0.0)
    except (TypeError, ValueError):
        api_interval = 0.0
    interval = max(api_interval, MIN_INTERVAL_S)
    out = {"code": code, "url": friendly or verification, "verificationUrl": verification,
           "expiresAt": data.get("expires_at"), "window": round(left), "interval": interval,
           "_device_code": device_code, "_deadline": t0 + left, "_api_interval": api_interval}
    # The 6 digits are shown on screen by design; the log gets only their length, and the device
    # code not at all.
    log.info(COMPONENT, "start", result="PASS", status=status, code=core.REDACTED, code_len=len(code), url=out["url"],
             verification_url=verification, interval_s=interval, window_s=round(left), window_basis=basis,
             documented_interval=data.get("interval"), ms=(clock() - t0) * 1000)
    return out


def public(started: dict) -> dict:
    """What may be printed or handed to the sheet: everything but the device code and the clock."""
    return {k: v for k, v in started.items() if not k.startswith("_")}


def _retry_after(answered) -> float:
    """A 429's `Retry-After` in seconds; 0 when absent or not a number of seconds."""
    value = str(header(answered, "Retry-After") or "").strip()
    return float(value) if value.isdigit() else 0.0


def poll(http: Http, log: Log, device_code: str, attempt: int) -> tuple[str, str, str, float]:
    """One exchange attempt. Returns (state, token, detail, retry_after) with state one of pending,
    granted, gone, no_token, refused, unexpected or transient. `token` is empty unless state is
    granted; `retry_after` is the seconds a 429 asked for, else 0."""
    try:
        status, body, answered = http.request(f"{COMPONENT}.token", "POST", _url("/user/auth/device/token"),
                                       headers={"Content-Type": "application/json"},
                                       data=json.dumps({"device_code": device_code}).encode(), retries=1,
                                       expected=(400,))
    except StageError as exc:
        # The window is still open, so none of these ends the wait: a network that is down or timed
        # out, a connection dropped or reset while the answer was read, a truncated body, and a 200
        # whose body is not JSON (a proxy's or Cloudflare's HTML page). `Http.request` raises all of
        # them as StageError and, this being a POST, does not send them again itself.
        log.warn(COMPONENT, "poll", result="FAIL", attempt=attempt, state="transient",
                 cause=exc.data.get("reason") or "unreachable", error=f"{type(exc).__name__}: {exc}"[:300],
                 suggest="no usable answer while waiting; asking again")
        return "transient", "", str(exc), 0.0
    raw = body
    body = body if isinstance(body, dict) else {}
    err, detail = str(body.get("error") or ""), str(body.get("detail") or "")
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    token = str(data.get("access_token") or "") if status == 200 and body.get("success") is not False else ""
    if token:
        state = "granted"
    elif err == PENDING:
        state = "pending"
    elif err == GONE:
        state = "gone"
    elif err == NO_TOKEN:
        state = "no_token"
    elif err in TRANSIENT or status in (429, 500, 502, 503, 504) or (status == 200 and not err):
        state = "transient"
    elif err and any(w in err.upper() for w in DENIAL_WORDS):
        state = "refused"
    else:
        state = "unexpected"
        if not err and not detail:
            detail = f"HTTP {status}" + (f": {str(raw)[:120]}" if raw else "")
    # Never the body on a grant: it is the token, and the scrubber does not know it yet.
    # "Not approved yet" is the normal answer for most of the wait: debug, not a line per poll at info.
    log.emit("debug" if state == "pending" else "info" if state == "granted" else "warn", COMPONENT, "poll",
             result="PASS" if state in ("pending", "granted") else "FAIL", attempt=attempt, status=status,
             state=state, error=err or None, detail=None if state == "granted" else detail[:200] or None,
             token_type=data.get("token_type") if state == "granted" else None)
    return state, token, detail, _retry_after(answered) if status == 429 else 0.0


def wait_for_token(http: Http, log: Log, started: dict, on_event: Callable[[dict], None], *,
                   sleep: Callable[[float], None] | None = None,
                   clock: Callable[[], float] | None = None) -> str:
    """Poll until TorBox hands over the token, the window closes, or TorBox refuses. The wait backs
    off (×1.25 per pending answer, ×2 per transient failure) between `MIN_INTERVAL_S` and
    `MAX_INTERVAL_S`, and never sleeps past the deadline: the last ask lands on it. TorBox's own
    pacing wins over both bounds: an `interval` above the cap is kept on every sleep, and a 429's
    `Retry-After` is waited (up to `RETRY_AFTER_MAX_S`)."""
    sleep, clock = sleep or SLEEP, clock or CLOCK
    device_code, deadline = started["_device_code"], started["_deadline"]
    api_interval = float(started.get("_api_interval") or 0)
    cap = max(MAX_INTERVAL_S, api_interval)
    interval = max(float(started.get("interval") or 0), MIN_INTERVAL_S, api_interval)
    attempt = 0
    while True:
        left = deadline - clock()
        if left <= 0:
            on_event({"state": "expired"})
            log.error(COMPONENT, "wait", result="FAIL", state="expired", attempts=attempt,
                      suggest="the code was not approved within 10 minutes; " + AGAIN)
            raise StageError(f"{COMPONENT}.wait", "The TorBox code expired before it was approved", AGAIN,
                             deviceState="expired")
        sleep(min(interval + random.uniform(0, 0.5), left))
        attempt += 1
        state, token, detail, retry_after = poll(http, log, device_code, attempt)
        if state == "granted":
            on_event({"state": "granted"})
            log.info(COMPONENT, "wait", result="PASS", state="granted", attempts=attempt)
            return token
        if state == "gone":
            left_now = deadline - clock()
            early = left_now > GONE_EARLY_S
            final = "denied" if early else "expired"
            on_event({"state": final})
            # The decision's inputs are on the line: this is the one to read to learn what a
            # declined sign-in really answers.
            log.error(COMPONENT, "wait", result="FAIL", state=final, attempts=attempt, detail=detail[:200],
                      left_s=round(left_now, 1), gone_early_s=GONE_EARLY_S,
                      suggest=("TorBox dropped the code before its 10 minutes were up: declined or cancelled"
                               if early else "the code expired") + "; " + AGAIN)
            raise StageError(f"{COMPONENT}.wait",
                             "The TorBox sign-in was declined or cancelled" if early
                             else "The TorBox code expired before it was approved", AGAIN, deviceState=final)
        if state == "no_token":
            on_event({"state": "failed"})
            log.error(COMPONENT, "wait", result="FAIL", state="no_token", attempts=attempt, detail=detail[:200],
                      suggest="the account has no API token yet: create one at torbox.app/settings, then sign in again")
            raise StageError(f"{COMPONENT}.wait", "TorBox approved the sign-in but has no API token for this account",
                             "create an API token at https://torbox.app/settings, then " + AGAIN,
                             deviceState="failed",
                             # For the sheet, where "run it again" is a button, not a command.
                             deviceHint="Create an API token at https://torbox.app/settings, then get a new code")
        if state == "unexpected":
            on_event({"state": "failed"})
            log.error(COMPONENT, "wait", result="FAIL", state="unexpected", attempts=attempt, detail=detail[:200],
                      suggest="TorBox answered with something this plugin does not know (see the poll line); " + AGAIN)
            raise StageError(f"{COMPONENT}.wait", "TorBox answered unexpectedly" + (f": {detail[:120]}" if detail else ""),
                             AGAIN, deviceState="failed")
        if state == "refused":
            on_event({"state": "denied"})
            log.error(COMPONENT, "wait", result="FAIL", state="refused", attempts=attempt, detail=detail[:200],
                      suggest="TorBox's error code reads as a declined sign-in; " + AGAIN)
            raise StageError(f"{COMPONENT}.wait", "TorBox refused the sign-in" + (f": {detail[:120]}" if detail else ""),
                             AGAIN, deviceState="denied")
        on_event({"state": state, "attempt": attempt, "left": max(0, round(deadline - clock()))})
        interval = max(min(interval * (2 if state == "transient" else BACKOFF), cap), api_interval)
        if retry_after:
            interval = max(interval, min(retry_after, RETRY_AFTER_MAX_S))
        log.debug(COMPONENT, "backoff", state=state, attempt=attempt, next_interval_s=round(interval, 2),
                  retry_after_s=retry_after or None, cap_s=cap, left_s=round(deadline - clock(), 1))


def parse_ascii_qr(ascii_art: str) -> list[str]:
    """`qrencode --type ASCII` draws a dark module as `##` and a light one as two spaces; one row
    of "1"/"0" per line."""
    return ["".join("1" if "#" in line[i:i + 2] else "0" for i in range(0, len(line), 2))
            for line in ascii_art.splitlines()]


def qr_rows(text: str, log: Log) -> list[str]:
    """The QR code for `text` as rows of "1" (dark) and "0" (light), quiet zone included, from
    `qrencode` — which Omarchy installs for its own Wi-Fi QR panel (`omarchy-base.packages`), so
    this adds no dependency. The input goes over stdin, not argv. Empty when it cannot be drawn:
    the code and the short URL are enough on their own."""
    exe = shutil.which("qrencode")
    if not exe or not text:
        log.info(COMPONENT, "qr", result="SKIP", reason="no qrencode" if not exe else "no url")
        return []
    try:
        proc = subprocess.run([exe, "--type", "ASCII", "--margin", "4", "--output", "-"], input=text,
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warn(COMPONENT, "qr", result="FAIL", error=str(exc), suggest="showing the code and URL only")
        return []
    rows = parse_ascii_qr(proc.stdout)
    if proc.returncode != 0 or not rows or any(len(r) != len(rows) for r in rows):
        log.warn(COMPONENT, "qr", result="FAIL", rc=proc.returncode, rows=len(rows),
                 stderr=proc.stderr[:200], suggest="showing the code and URL only")
        return []
    log.info(COMPONENT, "qr", result="PASS", size=len(rows))
    return rows
