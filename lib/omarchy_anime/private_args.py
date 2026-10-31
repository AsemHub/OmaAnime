"""The words of a launch that say what is watched travel in the environment, never on the command line.

A process's command line is readable by every account on the computer (`/proc/<pid>/cmdline` is mode 0444, and
Omarchy mounts `/proc` without `hidepid`); its environment only by its owner (`/proc/<pid>/environ`, 0400). So a
launch of `omarchy-anime` keeps fixed words on its command line (the binary, `--trace` and the trace, the subcommand)
and hands every other word (an AniList id, a title, a release or file name, an info hash, search text) in
`OMAANIME_ARGS`, a JSON list of strings. `bin/omarchy-anime` reads it before anything else, appends its words to the
argv and removes it from its own environment, so no child inherits it. The shell (`Logic.cliLaunch`) and the player
script (`cli_command`) build the same variable; `command` here is the Python side's one builder.

Stdlib only: the entry imports this ahead of the lean `status` path and of `core`.
"""
import json
import os
import random
import sys
import time
from pathlib import Path

ENV = "OMAANIME_ARGS"
BIN = Path(__file__).resolve().parents[2] / "bin" / "omarchy-anime"


class Refused(ValueError):
    """`OMAANIME_ARGS` is not a JSON list of strings."""


def take(argv: list[str], environ=None) -> list[str]:
    """`argv` followed by the variable's words. The variable leaves `environ` (default `os.environ`, which unsets it
    for every child) first, whatever it holds; `Refused` when it is not a JSON list of strings."""
    environ = os.environ if environ is None else environ
    raw = environ.pop(ENV, None)
    if raw is None:
        return list(argv)
    try:
        words = json.loads(raw)
    except ValueError as exc:
        raise Refused(f"{ENV} is not JSON ({exc})") from None
    if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
        what = "a list with a non-string item" if isinstance(words, list) else type(words).__name__
        raise Refused(f"{ENV} is not a JSON list of strings (got {what})")
    return [*argv, *words]


def refuse(argv: list[str], exc: Refused) -> int:
    """A malformed variable: one `cli/args` FAIL line under the argv's trace (or a new one), the CLI's JSON error, 2.
    The variable's text is not logged: it may hold the words it was meant to keep off the command line."""
    from .core import Log, safe_argv                     # noqa: PLC0415 — only on this path
    from .status import emit_json                        # noqa: PLC0415
    given = argv[argv.index("--trace") + 1] if "--trace" in argv[:-1] else \
        next((a[len("--trace="):] for a in argv if a.startswith("--trace=")), "")
    trace = given or os.environ.get("OMARCHY_ANIME_TRACE") or f"cli-{int(time.time())}-{random.randrange(16**4):04x}"
    Log(trace).error("cli", "args", result="FAIL", argv=safe_argv(argv), reason=str(exc),
                     suggest=f"whatever started the CLI set {ENV} wrongly; nothing was run")
    emit_json({"ok": False, "trace": trace, "stage": "cli.args", "error": str(exc),
               "suggest": f"{ENV} must be a JSON list of strings"})
    return 2


def command(head: list[str], words: list[str], environ=None) -> tuple[list[str], dict]:
    """One launch of the plugin's own CLI by Python: `(argv, env)`, the argv `[python, bin, *head]` and the env a copy
    of `environ` (default `os.environ`) with `words` in the variable. `head` holds fixed words and the trace only."""
    env = dict(os.environ if environ is None else environ)
    env[ENV] = json.dumps([str(w) for w in words])
    return [sys.executable, str(BIN), *[str(h) for h in head]], env
