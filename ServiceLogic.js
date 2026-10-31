// Pure decision logic for Service.qml. Qt-free on purpose: `import "ServiceLogic.js" as Logic`
// from Service.qml, and node's test runner runs this same source against fixtures taken from
// live CLI output. Same arrangement as BrowseModel.js, and for the
// same reason: none of this is reachable from a QML test, so it is written where node can reach it.
//
// A dead mpv socket is why this file exists: one used to disable every progress write in the
// plugin without a single log line, so the rules that decide "is the socket really down", "may the
// status poll drive the tick instead" and "how loud should that be" belong somewhere they can be
// unit-tested, not inside a Timer binding.

// Reconnect backoff (throttle, never a cliff edge). One attempt per second at first, then
// doubling to one per fifteen — fast enough that a player restart is invisible, slow enough that a
// player that stays down costs four attempts a minute.
var BACKOFF_MIN_MS = 1000
var BACKOFF_MAX_MS = 15000

function backoffNext(interval, maxMs) {
  var cap = Number(maxMs) > 0 ? Number(maxMs) : BACKOFF_MAX_MS
  var cur = Number(interval)
  if (!isFinite(cur) || cur < BACKOFF_MIN_MS) cur = BACKOFF_MIN_MS
  return Math.min(cur * 2, cap)
}

// -------------------------------------------------------------------------- fallback tick
// The socket must not be the only thing that can write progress. When it is down but the CLI says
// mpv is running with a file loaded, the 10 s `omarchy-anime status` poll drives the same `tick`
// the socket would have driven, from the position that poll already carries. Python owns the
// watched rule and is idempotent (player.watched_decision returns "already" once resume.json has
// watched:true), so a tick that overlaps a socket-driven one costs one extra file write, never a
// second AniList write.
//
// Field names are the ones `omarchy-anime status` actually emits, read off live output on
// 2026-09-16 (`./plugin/bin/omarchy-anime status | jq 'keys'`):
//   running, responsive, parked, visible, pid, pause, time-pos, duration, percent-pos,
//   media-title, idle-active, eof-reached, paused-for-cache, nowPlaying{mediaId,episode,trace,…}
function fallbackTick(status, socketConnected, nowPlaying) {
  var out = { tick: false, pos: 0, dur: 0, eof: false, reason: "" }
  // The socket owns the cadence whenever it is alive; this is strictly the understudy.
  if (socketConnected === true) { out.reason = "socket-up"; return out }
  if (!status || typeof status !== "object") { out.reason = "no-status"; return out }
  if (status.running !== true) { out.reason = "mpv-down"; return out }
  // A running but unresponsive core answers nothing, so its position is not a fact.
  if (status.responsive === false) { out.reason = "mpv-unresponsive"; return out }
  if (status["idle-active"] !== false) { out.reason = "idle"; return out }
  if (!nowPlaying || !(Number(nowPlaying.mediaId) > 0) || !(Number(nowPlaying.episode) > 0)) {
    out.reason = "nothing-tracked"
    return out
  }
  var pos = Number(status["time-pos"])
  var dur = Number(status.duration)
  if (!isFinite(pos) || pos < 0) pos = 0
  if (!isFinite(dur) || dur < 0) dur = 0
  out.eof = status["eof-reached"] === true
  // A tick at 0 s would only overwrite the resume position with the start of the file; the one
  // exception is eof, where the watched rule still has to run. At eof the position decides
  // watched or cut, and the CLI judges it by what mpv reports when mpv answers (`do_tick`), so a
  // missing time-pos sent as 0 here is not what decides it.
  if (!out.eof && !(pos > 0 && dur > 0)) { out.reason = "no-position"; return out }
  out.tick = true
  out.pos = pos
  out.dur = dur
  out.reason = "fallback"
  return out
}

// ------------------------------------------------------------------------------ socket down notice
// A socket that cannot reach an mpv that is demonstrably running is a fault, and it must never be
// silent: a silent one once lost every progress write. The grace
// period exists because an mpv restart legitimately drops the socket for a few seconds — long
// enough to matter, short enough that a notice would be noise. Past it, the popout says so and the
// JSONL gets a warn line.
var SOCKET_DOWN_GRACE_MS = 60000

function socketDownSeconds(downSinceMs, nowMs) {
  var since = Number(downSinceMs)
  var now = Number(nowMs)
  if (!isFinite(since) || since <= 0 || !isFinite(now) || now < since) return 0
  return Math.floor((now - since) / 1000)
}

function socketDownText(seconds) {
  var howLong = seconds < 120 ? seconds + "s" : Math.floor(seconds / 60) + " min"
  return "Lost the live player connection " + howLong
       + " ago — progress is being tracked from the status poll · omarchy-anime player start"
}

function socketDownNotice(downSinceMs, nowMs, mpvRunning, graceMs) {
  var out = { warn: false, seconds: 0, text: "" }
  // Nothing to warn about while mpv itself is gone: the socket is *supposed* to be down then, and
  // the popout already shows "nothing playing".
  if (mpvRunning !== true) return out
  out.seconds = socketDownSeconds(downSinceMs, nowMs)
  if (out.seconds <= 0) return out
  var grace = Number(graceMs)
  if (!isFinite(grace) || grace < 0) grace = SOCKET_DOWN_GRACE_MS
  if (out.seconds * 1000 < grace) return out
  out.warn = true
  out.text = socketDownText(out.seconds)
  return out
}

// ----------------------------------------------------------- the socket's down clock and attempts
// The down clock used to start at the first failed attempt, and the attempts never stopped. With no player at all
// (a fresh shell start), the first play's first poll then found the socket "down for 14 min" (`downSeconds` 867) and
// warned, and the shell log took a `ServerNotFoundError` WARN every ~15 s all session. Now the clock runs only while
// a player runs (a poll that sees mpv, or a live connection that drops), and the socket is rebuilt only while there
// may be a player to reach: a recent poll that saw none stands the attempts down. A missing or old poll answer never
// does (the socket's fallback is that poll, so an unknown state keeps trying, with the backoff).
var SOCKET_EXPECT_MS = 10000      // after this plugin starts or shows a player, before any poll can have seen it
var SOCKET_NO_PLAYER_FRESH_MS = 30000   // three 10 s polls: a "no player" answer older than that is not trusted

function socketInit() {
  return { downSince: 0, attempts: 0, warned: false, noPlayerAt: 0, expectUntil: 0, standing: false }
}

// Whether the reconnect timer that just fired builds a socket. `s` is the `socketStep` state.
function socketAttemptDecision(s, nowMs, connected, path) {
  if (connected === true) return { attempt: false, reason: "connected" }
  if (!path) return { attempt: false, reason: "no-path" }
  if (nowMs < s.expectUntil) return { attempt: true, reason: "player-expected" }
  if (s.noPlayerAt > 0 && nowMs - s.noPlayerAt <= SOCKET_NO_PLAYER_FRESH_MS)
    return { attempt: false, reason: "no-player" }
  return { attempt: true, reason: s.noPlayerAt > 0 ? "no-player-stale" : "player-maybe" }
}

// One event on the socket's state. `ev.type`:
//   expect        this plugin started or showed a player (`ev.why`): attempts for SOCKET_EXPECT_MS whatever a poll says
//   due           the reconnect timer fired (`ev.connected`, `ev.path`): `attempt` says whether to build a socket
//   connected     the socket connected
//   disconnected  a live connection dropped: the player was there a moment ago, so the clock starts
//   poll          `omarchy-anime status` answered (`ev.running`, `ev.connected`). `running` true or false is a fact;
//                 anything else (a failed `status`: `{ok: false, …}` has no `running`) is unknown and changes
//                 nothing (an error is not "no player"; it neither stands the attempts down nor ends an
//                 episode)
// Returns {state, attempt, reason, opened, closed, log, warn, notice}: `opened` when a down episode (and its trace)
// starts, `closed` when it ends; `log` a line to write before the trace is dropped; `warn` the notice the first time
// it crosses the grace (the Service adds its own fields to that line); `notice` is `socketDownNotice` (poll only).
function socketStep(state, ev) {
  var s = {}
  var base = state && typeof state === "object" ? state : socketInit()
  var init = socketInit()
  for (var k in init) s[k] = base[k] === undefined ? init[k] : base[k]
  var now = Number(ev && ev.now) || 0
  var out = { state: s, attempt: false, reason: "", opened: false, closed: false, log: null, warn: null, notice: null }
  var type = ev ? String(ev.type) : ""
  function episode() {
    return { downSeconds: socketDownSeconds(s.downSince, now), attempts: s.attempts, warned: s.warned }
  }
  function close() {
    s.downSince = 0
    s.attempts = 0
    s.warned = false
    out.closed = true
  }
  function open() {
    s.downSince = now
    s.attempts = 0
    s.warned = false
    out.opened = true
  }
  if (type === "expect") {
    s.expectUntil = Math.max(s.expectUntil, now + SOCKET_EXPECT_MS)
    out.reason = String(ev.why || "")
  } else if (type === "due") {
    var d = socketAttemptDecision(s, now, ev.connected, ev.path)
    out.attempt = d.attempt
    out.reason = d.reason
    if (d.attempt) {
      if (s.downSince > 0) s.attempts += 1
      // The step back from a stand-down is said too, with why (a play, a player that
      // a poll saw, or a "no player" answer gone stale because the poll stopped answering).
      if (s.standing)
        out.log = { stage: "socket.resume", level: "info",
                    data: { reason: d.reason, noPlayerAgoMs: s.noPlayerAt > 0 ? now - s.noPlayerAt : null,
                            expectLeftMs: Math.max(0, s.expectUntil - now) } }
      s.standing = false
    } else if (!s.standing && d.reason === "no-player") {
      // Once per stand-down: the timer stops here, and the next poll or play wakes it.
      s.standing = true
      out.log = { stage: "socket.standby", level: "info",
                  data: { reason: d.reason, noPlayerAgoMs: now - s.noPlayerAt } }
    }
  } else if (type === "connected") {
    if (s.downSince > 0) {
      // Only an outage is restored. A first connection (a shell start with mpv already up,
      // a first play) costs one attempt and never warned, and writes no line.
      if (s.warned || s.attempts > 1) out.log = { stage: "socket.restored", level: "info", data: episode() }
      close()
    }
    s.noPlayerAt = 0
    s.standing = false
  } else if (type === "disconnected") {
    s.noPlayerAt = 0
    if (!(s.downSince > 0)) open()
  } else if (type === "poll") {
    var known = ev.running === true || ev.running === false
    // Unknown: the clock and the attempts go on as they were; an open episode means the player was last seen up.
    var running = known ? ev.running : s.downSince > 0
    if (!known) {
      out.reason = "unknown"
    } else if (!running) {
      s.noPlayerAt = now
      if (s.downSince > 0) {
        var ended = episode()
        ended.reason = "player-gone"
        out.log = { stage: "socket.ended", level: "info", data: ended }
        close()
      }
    } else {
      s.noPlayerAt = 0
      if (ev.connected !== true && !(s.downSince > 0)) open()
    }
    out.notice = socketDownNotice(s.downSince, now, running)
    if (out.notice.warn && !s.warned) {
      s.warned = true
      out.warn = out.notice
    }
  }
  return out
}

// `omarchy-anime <args>` from `Service.run`: whether it starts or shows the player, so the socket is tried at once.
function socketExpects(args) {
  if (!isList(args) || String(args[0]) !== "player") return false
  return ["start", "restart", "show", "toggle"].indexOf(String(args[1])) >= 0
}

// ------------------------------------------------------------------------- stale player notice
// mpv reads `--script=` once, at process start. A player that has been running since before the
// Lua script was last changed is executing the *previous* copy of it — the panel still opens, the
// socket still answers, `user-data` still says the script is loaded, and half the feature silently
// does nothing. The CLI owns the comparison (`player.staleness` in status's `stale` key, process
// start time vs. the mtime of every file the process loaded at startup); this turns it into the one
// sentence the popout shows, and names the command that ends it.
//
// Ranked directly under the socket-down notice and above the token/rate ones for the same reason
// that one is first: it describes something wrong *now* that one command fixes, where the others
// will still be true tomorrow.
function staleNotice(status) {
  var out = { warn: false, text: "", file: "", reason: "" }
  if (!status || typeof status !== "object" || status.running !== true) return out
  var st = status.stale
  if (!st || typeof st !== "object" || st.stale !== true) return out
  out.warn = true
  out.reason = String(st.reason || "")
  var path = st.newest && st.newest.path ? String(st.newest.path) : ""
  out.file = path ? path.split("/").pop() : ""
  out.text = "The player has been running since before " + (out.file || "its own script")
           + " changed, so it is still using the older copy — run: omarchy-anime player restart"
  return out
}

// ------------------------------------------------------------------------ the in-flight play
// `omarchy-anime status` carries `play`: the entry the CLI writes while it assembles a play, and
// leaves behind when one fails. It is the same file the in-player panel reads, which is the point —
// before it, each surface knew only about the plays it had started itself. A play from the panel
// left this service's `busy` empty for its whole 28-43 s, and the one that failed was reported
// nowhere at all.
//
// An entry nobody has updated for this long is not evidence that anything is still happening. It is
// the same number as `playstate.ABANDON_AFTER_S`, and for the same reason: past it, the claim is
// taken over rather than believed.
var PLAY_ABANDON_S = 20

function playBusyText(play) {
  if (play.text) return String(play.text)
  // `text` is the CLI's own rendering. Fall back to the parts, so an older CLI (or a file written
  // by hand during a test) still produces a sentence instead of an empty busy line.
  var secs = Math.floor(Number(play.elapsed) || 0)
  var line = "Episode " + play.episode + " — " + (play.message || "starting")
  return secs >= 1 ? line + " · " + secs + "s" : line
}

// `mine` is whether this service's own play Process is running: its error carries the candidate
// list and the retry binding, so it stays the authority until it exits. `seenTrace` is the last
// failure this service already showed, so one failure is adopted once and a dismissed error is not
// resurrected by the next poll. `nowSec` is a parameter for the same reason it is one in
// `socketDownNotice`: a clock read inside a pure function is a test that rots.
// ----------------------------------------------------------------------- the failure's two halves
// The CLI's `suggest` is "<what to do> · <what each provider answered>". The action is the half a
// user acts on and the half the narrow surfaces (the bar popout's one line, the player's three
// wrapped lines) have room for; the stage dump behind it is how the failure is diagnosed afterwards
// and is shown where there is width for it. The separator is ` · ` and Python owns it
// (`cli.describe_probe_errors`), which is why this is one split and not a parser.
function errorAction(suggest) {
  var s = String(suggest || "")
  var cut = s.indexOf(" · ")
  return cut < 0 ? s : s.substring(0, cut)
}

function errorStages(suggest) {
  var s = String(suggest || "")
  var cut = s.indexOf(" · ")
  return cut < 0 ? "" : s.substring(cut + 3)
}

// The one place either failure path — the poll's `Logic.playNotice`
// or the CLI's own direct JSON response (`playProc` in Service.qml) — turns `say` plus the old
// record fields into what a person reads. `say` is always an object but may be empty, so every
// field falls back on its own; testing the object's truthiness alone would treat `{}` as sentenced.
function errorSentences(say, fallbackError, fallbackSuggest) {
  var said = say && typeof say === "object" ? say : null
  return {
    headline: String((said && said.headline) || fallbackError),
    action: String((said && said.action) || errorAction(fallbackSuggest)),
    hint: String((said && said.hint) || ""),
  }
}

// A play that stopped waiting for TorBox and holds the viewer's choices (`playstate.HELD`) is neither busy nor
// failed: `held` is its plain sentence (the CLI's rendering), which the popout shows dimmed and without "Copy trace",
// and Browse shows above the choices' buttons. Its process has exited, so no Process of this Service is running it.
// `later` is a play the viewer asked to be told about ("Tell me when it's ready"): its acknowledgement, "We'll tell you
// when episode 3 is ready.", for the popout. Both belong to their moment: `HELD_SHOW_S` after `finishedAt` they are
// gone (the number is `playstate.HELD_SHOW_S`).
var HELD_SHOW_S = 3600

function playNotice(play, mine, seenTrace, nowSec) {
  var out = { busy: "", error: "", stage: "", suggest: "", trace: "", adopt: false,
              headline: "", action: "", hint: "", held: "", later: "" }
  if (!play || typeof play !== "object" || !play.state) return out
  if (mine === true) return out
  out.trace = String(play.trace || "")
  if (play.state === "held" || play.state === "later") {
    var at = Number(nowSec)
    if (!isFinite(at) || at <= 0) at = Date.now() / 1000
    if (at - (Number(play.finishedAt) || 0) > HELD_SHOW_S) return out
    if (play.state === "later") out.later = String(play.message || "")
    else out.held = play.text ? String(play.text) : "Episode " + play.episode + " — " + String(play.message || "")
    return out
  }
  if (play.state === "running") {
    var now = Number(nowSec)
    if (!isFinite(now) || now <= 0) now = Date.now() / 1000
    var quiet = now - (Number(play.updatedAt) || 0)
    if (quiet > PLAY_ABANDON_S) return out          // the writer went away; say nothing
    out.busy = playBusyText(play)
    return out
  }
  if (play.state === "failed") {
    if (out.trace !== "" && out.trace === String(seenTrace || "")) return out
    out.error = String(play.error || play.message || "the play failed")
    out.stage = String(play.failedStage || play.stage || "")
    out.suggest = String(play.suggest || "")
    out.adopt = true
    // The CLI now sends sentences beside the stage detail; `errorSentences` is
    // the fallback for a play-status written by an older CLI, where splitting on " · " was the
    // only way to find something sayable.
    var said = errorSentences(play.say, out.error, out.suggest)
    out.headline = said.headline
    out.action = said.action
    out.hint = said.hint
  }
  return out
}

// What `later` answered ("Tell me when it's ready"), from its JSON on stdout: `{later, message}`. What the viewer
// is told is its `message`: "We'll tell you when episode 3 is ready." only when the request was recorded, "That
// couldn't be saved, so the player keeps waiting." when it was not. An answer that cannot be read is a request not
// known to be recorded, said plainly.
var LATER_UNREAD = "That couldn't be confirmed, so the player keeps waiting. Try again in a moment."

function laterAnswer(text) {
  var r = null
  try { r = JSON.parse(String(text || "")) } catch (e) { r = null }
  if (!r || typeof r !== "object") return { later: false, message: LATER_UNREAD }
  var message = typeof r.message === "string" && r.message !== "" ? r.message : LATER_UNREAD
  return { later: r.later === true, message: message }
}

// Whether a play asked for while this Service's own play is still running goes ahead anyway: that play is
// waiting for TorBox (`detail.torboxWait`, written within `PLAY_ABANDON_S`), which can last hours, and the new one is
// another episode or show, or a release the viewer picked for this episode while the wait offers its choices. The
// CLI's claim supersedes the waiting play in both cases (`playstate.decide`: a chosen release over a play with its
// choices); the download carries on in TorBox. The same episode with no release is still refused here: the CLI would
// only answer that it is already starting.
function playOverWait(play, ownTrace, mediaId, episode, nowSec, release) {
  if (!play || typeof play !== "object" || play.state !== "running") return false
  if (!play.detail || play.detail.torboxWait !== true) return false
  if (!ownTrace || String(play.trace || "") !== String(ownTrace)) return false
  var now = Number(nowSec)
  if (!isFinite(now) || now <= 0) now = Date.now() / 1000
  if (now - (Number(play.updatedAt) || 0) > PLAY_ABANDON_S) return false
  var same = Number(play.mediaId) === Number(mediaId) && Number(play.episode) === Number(episode)
  return !same || (!!release && !!play.choice)
}

// ------------------------------------------------------------------------- the CLI's private words
// A command line is readable by every account on the computer (`/proc/<pid>/cmdline`, 0444); an environment only by
// its owner (`/proc/<pid>/environ`, 0400). So a launch of the plugin's CLI puts fixed words on its command line (the
// binary, `--trace` and the trace, the subcommand) and every other word, the show, an episode, a release, a search, a
// log line's data, in `OMAANIME_ARGS`, a JSON list the CLI appends to its argv and drops from its environment before
// it starts anything (`bin/omarchy-anime`, `private_args.py`; `docs/privacy.md`). A test holds every command array in
// the QML, the JS and the player script to that.
var ARGS_ENV = "OMAANIME_ARGS"

// One launch: Quickshell's `execDetached` object form, `{command, environment}` (the calling file must import
// Quickshell.Io, or the object form throws), whose environment is added to the shell's. Always the variable, `[]`
// when there are no words.
function cliLaunch(head, words) {
  var env = {}
  env[ARGS_ENV] = JSON.stringify(listCopy(words).map(String))
  return { command: listCopy(head).map(String), environment: env }
}

// The same launch on a Process, both properties every time, so a reused Process never hands a run the last run's
// words. Started by the caller (`running = true`).
function cliOn(proc, head, words) {
  var launch = cliLaunch(head, words)
  proc.environment = launch.environment
  proc.command = launch.command
  return launch
}

// One `log-event` line: the component, the stage, the level and `data` (which names shows and releases) in the
// variable. Browse launches it itself for what must not go through the Service's `logEvent` (a stale Service may be
// the thing being reported).
function logEventLaunch(bin, trace, component, stage, data, level) {
  return cliLaunch([bin, "--trace", trace, "log-event"],
                   [String(component), String(stage), "--level", String(level || "info"),
                    "--data", JSON.stringify(data || {})])
}

// The CLI's subcommands a choice runs. One of them stays on the command line: a play is told from other processes by
// the word `play` there (`playstate.pid_is_play`), and the word says nothing about the show.
var CHOICE_COMMANDS = ["play", "later", "keep-waiting", "player"]

// The launch one of a waiting play's choices runs (`cli.wait_choice`'s `args`, Browse's button and the player's key
// alike): the plugin's binary, the trace the click was logged under and the subcommand, then the command's other words
// in the variable (they name the show and the release). null when there is none.
function choiceLaunch(bin, trace, args) {
  if (!bin || !isList(args) || args.length === 0) return null
  var words = listCopy(args).map(String)
  var at = CHOICE_COMMANDS.indexOf(words[0])
  if (at < 0) return cliLaunch([bin, "--trace", trace], words)
  return cliLaunch([bin, "--trace", trace, CHOICE_COMMANDS[at]], words.slice(1))
}

// Why `choiceLaunch` gives nothing to run, for the log; "" when it gives a launch.
function choiceRefusal(bin, args) {
  if (!bin) return "no plugin binary"
  if (!isList(args)) return "the button's args are not a list (" + Object.prototype.toString.call(args) + ")"
  return args.length === 0 ? "the button has no command" : ""
}

// ------------------------------------------------------------------------- auto-next
// Whether the tick that just came back should start the next episode.
//
// The gate this replaces was `r.watched === true && eof`, and those two are never true together
// except by accident. `watched` is **edge-triggered**: `playermod.watched_decision` returns it
// once, on the tick that first crosses the 85% threshold, so that progress is written exactly
// once — and at 85% there is still a sixth of the episode to go, so `eof` is false. Every tick
// after it reports `watched=false, reason='already'`. By the time `eof` turns true the flag is
// spent and the advance is unreachable; measured, it fired on 1 of ~250 ticks in a three-episode
// session, on the one tick that jumped from below the threshold straight to the end.
//
// "This episode is finished" is therefore `watched || already` — the edge or the state it leaves
// behind. `tick` is the CLI's `tick` JSON; `state` is {autoNext, eof, lastAdvance}.
function autoNextDecision(tick, state) {
  var no = function (reason) { return { play: false, mediaId: 0, episode: 0, reason: reason } }
  if (!tick || !state) return no("no tick")
  if (!state.autoNext) return no("autoNext off")
  if (!state.eof) return no("not at end of file")
  // mpv reports eof for a stream that was cut as well as one that ended; the CLI tells them apart.
  if (tick.cut === true) return no("the stream stopped short of the end")
  if (tick.watched !== true && tick.already !== true) return no("episode not watched")
  var next = tick.next
  if (!next || !next.episode || next.aired !== true) return no("no aired next episode")
  var mediaId = Number(tick.mediaId)
  var episode = Number(next.episode)
  if (!(mediaId > 0) || !(episode > 0)) return no("no aired next episode")
  // At EOF the tick timer keeps firing every ten seconds. Without this latch a play that fails —
  // no seeders, provider down — is retried forever, several times a minute.
  var last = state.lastAdvance
  if (last && Number(last.mediaId) === mediaId && Number(last.episode) === episode)
    return no("already advanced to this episode")
  return { play: true, mediaId: mediaId, episode: episode, reason: "advance" }
}

// ------------------------------------------------------------------- progress without the player
// The stage stream used not to cover resolve (measured): nothing was emitted between `claim()`'s
// "start" and the first provider probe, which is where a play spends its slow seconds. `status.stage()`
// calls at the resolve phase boundaries (search, rank, each provider probed) give `play.stages`
// something to show.
//
// A stage name never reaches the viewer, so a *past* row is never the bare
// name in `seen[i]` — it is `play.stageText[name]`, the sentence `say.stage()` produced when that
// stage was current, which `playstate.py` records alongside `stages` for exactly this reader. A
// name with no entry (an older CLI, or a hand-written play-status.json) degrades to the bare name
// rather than throwing — the same floor this popout showed before the field existed.

// One definition of "a play is happening right now", read by the heal that replaces a hung player. `updatedAt`
// is clamped to zero against a clock skewed into the future (an NTP correction, an RTC mismatch
// after a dual boot), which would otherwise read as forever-fresh.
function playInFlight(play, nowSec) {
  if (!play || typeof play !== "object" || play.state !== "running") return false
  var now = Number(nowSec)
  if (!isFinite(now) || now <= 0) now = Date.now() / 1000
  return Math.max(0, now - (Number(play.updatedAt) || 0)) <= PLAY_ABANDON_S
}

// Whether the heal should replace a player that has stopped answering IPC.
//
// The heal exists because mpv's core can genuinely wedge. But `responsive` is one ping with a 3 s
// timeout (`cli.player_state`), and an mpv that is merely BUSY — demuxing a slow stream over HTTP —
// can miss it. Before this, the heal ran `player start`,
// which kills the hung pid and launches a fresh unloaded mpv, destroying the very player the
// in-flight play was loading into; the play then died with "mpv closed the socket".
//
// Measured on 2026-09-21: loads of 0.3-5.7 s survived on four earlier days; the first 12.3 s load
// was killed this way. So the heal stands down while the play that owns this player is still in
// flight. If mpv is genuinely hung the play fails on its own timeout, its record stops being
// in-flight, and the next poll heals it — later than before, but without killing a healthy load.
// The status answer's language names (code -> English name), which the settings sheet labels its
// language choices with; an answer without them keeps the names already held.
function languageNamesOf(status, current) {
  var names = status ? status.languageNames : null
  return names && typeof names === "object" && !isList(names) ? names : current
}

function shouldHealPlayer(status, healing, nowSec) {
  var out = { heal: false, reason: "" }
  if (!status || typeof status !== "object") { out.reason = "no-status"; return out }
  if (status.running !== true) { out.reason = "no-player"; return out }
  if (status.responsive !== false) { out.reason = "responsive"; return out }
  if (healing === true) { out.reason = "cooling-down"; return out }
  if (playInFlight(status.play, nowSec)) { out.reason = "play-in-flight"; return out }
  out.heal = true
  out.reason = "hung"
  return out
}

function progressRows(play, nowSec) {
  if (!play || typeof play !== "object" || play.state !== "running") return []
  var now = Number(nowSec)
  if (!isFinite(now) || now <= 0) now = Date.now() / 1000
  if (now - (Number(play.updatedAt) || 0) > PLAY_ABANDON_S) return []   // the writer went away
  var seen = isList(play.stages) ? play.stages.slice() : []
  var current = String(play.stage || "")
  if (current !== "" && seen.indexOf(current) < 0) seen.push(current)
  if (seen.length === 0) return []
  var stageText = play.stageText && typeof play.stageText === "object" ? play.stageText : {}
  // `playstate.py` now keeps a returning stage (a stage that comes
  // back after another one) at its ORIGINAL position rather than appending it a second
  // time, but this reader stays robust for a hand-written or older file where `seen` still holds a
  // name twice — only the LAST occurrence is "the one it is on"; marking every match painted the
  // identical accented sentence twice with a dimmed row between them, reading as the play going
  // backwards.
  var currentAt = current !== "" ? seen.lastIndexOf(current) : -1
  var rows = []
  for (var i = 0; i < seen.length; i++) {
    var isCurrent = i === currentAt
    // The current row itself once fell back straight to the bare name when `play.message` was
    // empty, skipping `stageText`: a stage name reached the viewer.
    var text = isCurrent ? String(play.message || stageText[seen[i]] || seen[i])
                         : String(stageText[seen[i]] || seen[i])
    rows.push({ text: text, done: !isCurrent, current: isCurrent })
  }
  return rows
}

// The failures after which the release picker opens by itself. A refusal (the
// resolve would not choose), and every release failing to play, are exactly the moments a person
// choosing can do better than auto-play; a missing token or a player that would not load a file
// are not about releases at all.
var PICKER_STAGES = ["index.search", "score.prefilter", "score.ambiguous", "score.decision",
                     "score.file_pick", "resolve", "release.gone"]

// The picker's line when `releases` fails. The headline, then the action from
// `say` when the CLI sent one (the panel shows both too): "no indexer add-on" without its "add one
// to <folder>; Settings lists the ones that load" left the viewer nowhere to go. Without `say` the
// error alone, as before; the raw `suggest` is the log's, not a sentence.
function pickerErrorText(r) {
  if (!r || typeof r !== "object") return "no release list"
  var headline = String(r.error || r.stage || "no release list")
  var said = r.say && typeof r.say === "object" ? r.say : null
  var action = said && said.action ? String(said.action) : ""
  // With no indexer set up, where add-ons go (the hint) is part of the answer, as it is on the panel.
  var hint = said && said.hint && String(r.stage || "") === "indexers.none" ? " " + String(said.hint) : ""
  return action ? String(said.headline || headline) + " " + action + hint : headline
}

function wantsPicker(r) {
  if (!r || typeof r !== "object" || r.ok !== false) return false
  return PICKER_STAGES.indexOf(String(r.stage || "")) >= 0
}

// The release list's requests, the newest winning. One Process runs `releases`; its exit and the
// end of its stdout come in no guaranteed order (the TorBox device pattern), so a run is over only once both have
// been seen.
//   "start"   nothing is running: start now
//   "join"    the run in flight is this very request (same show, episode and words, not a refresh) and is not being
//             stopped: its answer is this one's. Restarting it would throw away a search half done (on
//             reopening the picker on the episode being searched)
//   "queue"   a run is in flight or ending: this request waits for it, replacing any request already waiting,
//             and the Service stops the run in flight
//   "refused" the CLI path is unknown
function releasesDecision(o) {
  o = o || {}
  if (!o.bin) return "refused"
  if (!o.running && o.exited !== false && o.streamDone !== false) return "start"
  if (o.running && !o.expectedStop && releasesSame(o.current, o.req)) return "join"
  return "queue"
}

// Whether request `b` asks exactly what the run for request `a` is fetching. A refresh never joins: it asks for a
// new search, which the run in flight may not be.
function releasesSame(a, b) {
  return !!a && !!b && b.refresh !== true && Number(a.mediaId) === Number(b.mediaId)
         && Number(a.episode) === Number(b.episode) && String(a.query || "") === String(b.query || "")
}

// Whether `data` (a `releases` answer) is the list for this episode. The sheet keeps a list only while it is the
// one asked for: an earlier episode's rows, filtered away by Browse, left it blank.
function pickerDataFor(data, mediaId, episode) {
  return !!data && typeof data === "object" && Number(data.anilist_id) === Number(mediaId)
         && Number(data.episode) === Number(episode)
}

// One `releases` stdout: {data} for a list, {error} for the sheet's "No list: …" line.
function releasesAnswer(text) {
  var r
  try {
    r = JSON.parse(String(text))
  } catch (e) {
    return { data: null, error: "the release list could not be read" }
  }
  if (!r || r.ok !== true) return { data: null, error: pickerErrorText(r) }
  return { data: r, error: "" }
}

// What the viewer reads when a play is asked for while this Service's own play is still starting.
// The running play is never replaced from here: the in-player panel's switch is a separate CLI process.
function playRefusalText(running, wanted) {
  var ep = Number(running && running.episode) || 0
  if (running && wanted && Number(running.mediaId) === Number(wanted.mediaId) && ep === Number(wanted.episode))
    return "Episode " + ep + " is already starting."
  return "Not started: " + (ep > 0 ? "episode " + ep : "another play") + " is still starting. "
         + "Ask again once it plays."
}

// AniList lists the entry as not yet released and gives no airing schedule: no episode of it is out. The Service's
// own copy of `BrowseModel.notOut` (the Service imports no Browse code); a node test holds the two equal over the
// shared cells cases.
function notOut(media) {
  if (!media || String(media.status || "") !== "NOT_YET_RELEASED") return false
  var next = media.nextAiringEpisode
  var ep = next ? next.episode : null
  return !(typeof ep === "number" && isFinite(ep) && Math.floor(ep) === ep && ep > 0)
}

// The episode the popout row's click and its ▶ play (`playNext`): the next one when it has aired, else the last
// watched (a rewatch), else null. Nothing for an entry that is not out, read off its media: a list saved before
// that rule still says its `next` aired.
function nextToPlay(entry) {
  if (!entry || !entry.media || notOut(entry.media)) return null
  var next = entry.next
  if (!next) return null
  return (next.aired ? next.episode : next.rewatch) || null
}

// Whether a play turned away before it started because the entry is not out (`playRefusal`) has outlived its
// moment, so its line goes:
//   entry          — the poll's play entry (`status.play`), or null
//   ownPlayRunning — this Service's own play is starting: a line said meanwhile ("already starting") is that play's
//                    to clear when it ends, never the timer's or the poll's
//   lastTrace      — the trace of the play this Service last asked for
//   beside         — the trace of the play that was running when the refusal came (`refusalBeside`): it does not
//                    clear the line, a play started after it does
//   timedOut       — the refusal's timer fired (`refusalClear`, 15 s)
function refusalOutlived(entry, ownPlayRunning, lastTrace, beside, timedOut) {
  if (ownPlayRunning) return false
  if (timedOut) return true
  if (!entry || entry.state !== "running") return false
  var trace = String(entry.trace || "")
  return trace !== String(lastTrace || "") && trace !== String(beside || "")
}

// --------------------------------------------------------------------------- list states
// Continue Watching, Lists and the popout used to say "Loading your list…" forever when AniList
// was signed out: the list process's error JSON was thrown away, and "not answered yet" and "will
// never answer" looked the same. These two functions are the whole decision; the QML only feeds
// them and draws `text`.
//
// Where the settings sheet is, whose top section is Accounts: the same words as the CLI's
// `say.SETTINGS_WHERE` (a test holds them equal). Not a key: nothing binds
// Super+A, which the first cut named.
var SETTINGS_WHERE = "Settings in the Anime bar popout"
var ACCOUNTS_LINE = "Open Accounts from " + SETTINGS_WHERE + "."
var SIGN_IN_AGAIN = "Sign in again: open Accounts from " + SETTINGS_WHERE + "."
// Where Settings is, said from where the line is read. The popout's own lines sit above
// its Settings button, the overlay has its ⚙ and `,`, and a line both of them show says only "Settings". Anywhere
// else (the player, a terminal) keeps SETTINGS_WHERE.
var SETTINGS_FROM = { popout: "Settings below", overlay: "Settings (⚙ or the , key)", shell: "Settings" }

function settingsWhere(where) {
  return Object.prototype.hasOwnProperty.call(SETTINGS_FROM, where) ? SETTINGS_FROM[where] : SETTINGS_WHERE
}
function accountsLine(where) { return "Open Accounts from " + settingsWhere(where) + "." }
function signInAgain(where) { return "Sign in again: open Accounts from " + settingsWhere(where) + "." }

// The bar's notice while the AniList token runs out. It points at Accounts, which every
// install has, not at a terminal command that is on PATH only after the README's one-time link. `where` as above:
// the Service's `notice` is drawn by the popout and the overlay alike, so it passes "shell".
function tokenWarningText(tok, where) {
  if (!tok || typeof tok !== "object" || tok.expiring !== true) return ""
  var days = Number(tok.daysLeft)
  if (tok.expired === true || !(days > 0)) return "Your AniList token has expired. " + signInAgain(where)
  return "Your AniList token expires in " + days + (days === 1 ? " day" : " days") + ". " + accountsLine(where)
         + " Replace it there."
}
// The CLI's `reason` for a list read that cannot succeed (`cli.do_anilist`, `AniList.query`).
var LIST_REASONS = ["signed_out", "token_expired", "token_rejected"]

// What a list command's output says went wrong, or null when it is a list. `r` is the parsed JSON,
// or null/undefined when the output did not parse. Read from fields (`reason`, then a transport
// stage `anilist.<stage>.http`), never from the words of `error`. `status` is
// the Lists status the read was for ("" for Continue Watching); see `listFailureFor`.
function listFailure(r, status) {
  if (listAnswerRows(r) !== null) return null
  var forStatus = String(status || "")
  if (!r || typeof r !== "object")
    return { state: "failed", error: "the list command printed no JSON", stage: "", reason: "", trace: "",
             status: forStatus }
  var reason = String(r.reason || "")
  var stage = String(r.stage || "")
  var state = "failed"
  if (LIST_REASONS.indexOf(reason) >= 0) state = reason
  else if (/^anilist\..*\.http$/.test(stage)) state = "network"
  return { state: state, error: String(r.error || (r.ok === false ? "" : "not a list")), stage: stage,
           reason: reason, trace: String(r.trace || ""), status: forStatus }
}

// The rows of a list command's answer: the bare list of a fresh read, or the `entries` of the saved list AniList's
// outage answered with (`{ok: true, entries, stale}`); null for anything else (a failure, no JSON).
function listAnswerRows(r) {
  if (isList(r)) return r
  if (r && typeof r === "object" && r.ok === true && isList(r.entries) && r.stale && typeof r.stale === "object")
    return r.entries
  return null
}

// The answer's stale marker, {savedAt, ageSeconds, reason}, when its rows are the saved list; null for a fresh read.
function listAnswerStale(r) {
  if (isList(r) || listAnswerRows(r) === null) return null
  var savedAt = Number(r.stale.savedAt)
  if (!(savedAt > 0)) return null
  return { savedAt: savedAt, ageSeconds: Number(r.stale.ageSeconds) || 0, reason: String(r.stale.reason || "") }
}

var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

function pad2(n) { return (n < 10 ? "0" : "") + n }

// When a saved list was read, in the viewer's local time: "14:05" today, "yesterday at 14:05", "Oct 20 at 14:05",
// and the year when it is not this year's.
function savedWhen(savedAtMs, nowMs) {
  var at = new Date(savedAtMs)
  var now = new Date(nowMs)
  var clock = pad2(at.getHours()) + ":" + pad2(at.getMinutes())
  var day = function (d) { return d.getFullYear() * 10000 + d.getMonth() * 100 + d.getDate() }
  if (day(at) === day(now)) return clock
  var yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  if (day(at) === day(yesterday)) return "yesterday at " + clock
  var date = MONTHS[at.getMonth()] + " " + at.getDate()
  if (at.getFullYear() !== now.getFullYear()) date += ", " + at.getFullYear()
  return date + " at " + clock
}

// The one line a list drawn from its saved copy shows above its rows, or "" for a fresh list. `o`:
//   stale  — `listAnswerStale(...)` of the answer the rows came from, or null
//   what   — "your list", "your Planning list"
//   nowMs  — now (Date.now() when absent)
// Why first, in the viewer's words: the cause is AniList's or the network's, never theirs.
// Then which list they are looking at, by when it was read. No code, no AniList sentence, no trace id.
// Two sentences: no connection or a timeout may be the viewer's own network ("Can't reach"); everything else is
// AniList's side, its rate limit included: the recorded outage had two HTML 429s among its 403s, and a sentence of
// their own would have flipped the line for ten minutes in the middle of it.
function listStaleText(o) {
  o = o || {}
  var stale = o.stale
  if (!stale || typeof stale !== "object" || !(Number(stale.savedAt) > 0)) return ""
  var reason = String(stale.reason || "")
  var why = reason === "unreachable" || reason === "timeout" ? "Can't reach AniList right now."
          : "AniList isn't answering right now."
  var nowMs = Number(o.nowMs) > 0 ? Number(o.nowMs) : Date.now()
  return why + " This is " + String(o.what || "your list") + " from " + savedWhen(Number(stale.savedAt) * 1000, nowMs)
         + "."
}

// Signed out, or a token AniList refuses: the rows already drawn belong to an account this plugin
// can no longer read, so they go. A network failure keeps them (the last good list beats
// an empty one).
function listFailureDropsRows(failure) {
  return !!failure && LIST_REASONS.indexOf(String(failure.state)) >= 0
}

// The failure the Lists tab shows for `status`. A network or other failure belongs to
// the status it was read for, so a cached status that loaded fine is not painted with another
// status's failure; signed out and a refused token are about the account, so every status shows them.
function listFailureFor(failure, status) {
  if (!failure) return null
  if (listFailureDropsRows(failure)) return failure
  return String(failure.status || "") === String(status || "") ? failure : null
}

// What an empty Continue Watching says once its list has loaded (the popout and the overlay alike).
var WATCHING_EMPTY = "Nothing on your AniList Watching list"

// The line an empty list view draws. `o`:
//   loaded     — a list has arrived for this view
//   loading    — a read is running now
//   failure    — `listFailure(...)` of the last answer, or null
//   what       — "your list", "your Planning list"
//   emptyText  — what a list that loaded with nothing on it says
//   where      — the surface drawing it, for where Settings is (`settingsWhere`)
// A failure outranks loading: the five-minute re-read must not turn "Sign in" back into "Loading"
// for the second it runs. The view shows `text` only while it has no rows, which is how a network
// failure over a list that loaded keeps showing the list.
function listStateText(o) {
  o = o || {}
  var what = String(o.what || "your list")
  var f = o.failure || null
  var detail = f && f.error ? String(f.error).slice(0, 240) : ""
  var lines
  var state
  if (f) {
    state = String(f.state || "failed")
    if (state === "signed_out") lines = ["Sign in to AniList to see " + what + ".", accountsLine(o.where)]
    else if (state === "token_expired") lines = ["Your AniList sign-in has expired.", signInAgain(o.where), detail]
    else if (state === "token_rejected") lines = ["AniList did not accept your sign-in.", signInAgain(o.where), detail]
    else if (state === "network") lines = ["Could not reach AniList to load " + what + ".", detail]
    else { state = "failed"; lines = ["Could not load " + what + ".", detail] }
  } else if (o.loading || !o.loaded) {
    state = "loading"
    lines = ["Loading " + what + "…"]
  } else {
    state = "empty"
    lines = [String(o.emptyText || "Nothing on " + what)]
  }
  return { state: state, text: lines.filter(function (l) { return l !== "" }).join("\n") }
}

// ---------------------------------------------------------------------- TorBox device sign-in
// `omarchy-anime auth torbox --device` writes one compact `{"event":"device",...}` line to stderr
// per step (among the JSON log lines, which carry no `event` key) and its result to stdout. These
// four functions turn that into the one object the Accounts sheet draws:
//   { state, code, url, verificationUrl, qr, left, error }
// state: idle | starting | waiting | granted | saved | expired | denied | failed | cancelled.
// The object never holds the device code (the CLI never prints it) or the token.
var DEVICE_TERMINAL = ["saved", "expired", "denied", "failed", "cancelled"]

function deviceIsTerminal(state) { return DEVICE_TERMINAL.indexOf(String(state)) >= 0 }

// Once TorBox has approved, the CLI is checking and saving the token: a Cancel there could stop it
// half way, so the sheet offers none until it ends.
function deviceCancellable(sign) { return !sign || String(sign.state) !== "granted" }

function deviceEvent(line) {
  var s = String(line || "")
  if (s.indexOf('"event"') < 0) return null
  var ev
  try { ev = JSON.parse(s) } catch (e) { return null }
  if (!ev || typeof ev !== "object" || ev.event !== "device" || typeof ev.state !== "string") return null
  return ev
}

// The sign-in page the sheet may show and open (`torbox_device.page_url`'s rule, which the CLI already applied):
// https on tor.box, torbox.app or a subdomain of it, no user part, the default port; else "". `xdg-open` opens any
// scheme's handler or a local file, so nothing else may reach it.
function devicePageUrl(value) {
  var s = String(value || "")
  var m = /^https:\/\/([^\/?#]*)([\/?#]|$)/i.exec(s)
  if (!m || /[\x00-\x20\x7f]/.test(s)) return ""
  var host = m[1].toLowerCase().replace(/:443$/, "")
  return /^(tor\.box|torbox\.app|[a-z0-9-]+(\.[a-z0-9-]+)*\.torbox\.app)$/.test(host) ? s : ""
}

function deviceReduce(sign, ev) {
  var cur = sign || ({ state: "idle" })
  if (!ev) return cur
  if (deviceIsTerminal(cur.state)) return cur          // a late line never reopens a finished sign-in
  var next = {}
  for (var k in cur) next[k] = cur[k]
  if (ev.state === "code") {
    next.state = "waiting"
    next.code = String(ev.code || "")
    next.url = devicePageUrl(ev.url)
    next.verificationUrl = devicePageUrl(ev.verificationUrl) || next.url
    var rows = isList(ev.qr) ? ev.qr.map(String) : []
    var square = rows.length > 0 && rows.every(function (r) { return r.length === rows.length && /^[01]+$/.test(r) })
    next.qr = square ? rows : []
    next.left = Number(ev.window) || 600
  } else if (ev.state === "pending" || ev.state === "transient") {
    next.state = "waiting"
    if (ev.left !== undefined) next.left = Number(ev.left)
    next.retrying = ev.state === "transient"
  } else if (ev.state === "granted" || ev.state === "saved" || ev.state === "expired"
             || ev.state === "denied" || ev.state === "failed") {
    next.state = ev.state
  }
  return next
}

// Whether a new device sign-in may start now (Get a new code, Sign in, Replace). The Process is
// reused, and its exit and the end of its stdout come in no guaranteed order (the wifiqr and search
// pattern), so a run is over only once both have been seen; until then a start is queued, never
// dropped, and whatever the ending run still says is not applied to the next one.
//   "start"   nothing is running: start now
//   "queue"   a run is stopping, or has told the sheet it is finished but not exited yet
//   "busy"    a live wait is running: the sheet already shows it
//   "refused" the CLI path is unknown
function deviceStartDecision(o) {
  o = o || {}
  if (!o.bin) return "refused"
  if (!o.running && o.exited !== false && o.streamDone !== false) return "start"
  if (o.expectedStop || deviceIsTerminal(o.state) || !o.running) return "queue"
  return "busy"
}

// Whether the device code a sign-in now shows goes on the clipboard: once per run (`trace`, the Service's
// device run, against `copiedTrace`, the run it last copied for), while it waits, and only a code that looks like one
// (TorBox's is 6 characters). The viewer pastes it at TorBox instead of typing it from the window.
function deviceCopyWanted(sign, trace, copiedTrace) {
  if (!sign || sign.state !== "waiting" || !trace || String(trace) === String(copiedTrace || "")) return false
  return /^[A-Za-z0-9]{4,12}$/.test(String(sign.code || ""))
}

// What the TorBox row says of the copy, from `wl-copy`'s exit status: "copied" only for 0, so a failed copy never
// tells the viewer to paste whatever the clipboard held before.
function deviceClipResult(code) {
  return code === 0 ? "copied" : "failed"
}

// The stdout result: `{saved: true, ...}` or `{ok: false, error, suggest, deviceState}`. It
// decides the end state even when the stderr line for it was lost, and adds the error text. A
// cancelled run stays cancelled, unless it saved a token before it stopped: the disk wins, so the
// sheet never says "Cancelled." over a new sign-in.
function deviceFinish(sign, result) {
  var cur = sign || ({ state: "idle" })
  if (cur.state === "cancelled" && !(result && result.saved === true)) return cur
  var next = {}
  for (var k in cur) next[k] = cur[k]
  if (result && result.saved === true) {
    next.state = "saved"
    next.error = ""
    next.hint = ""
  } else {
    var st = result && typeof result.deviceState === "string" ? result.deviceState : "failed"
    next.state = deviceIsTerminal(st) ? st : "failed"
    // Empty when there is no reason to quote; deviceStatusText supplies the fallback sentence.
    next.error = result && result.error ? String(result.error).slice(0, 240) : ""
    next.hint = result && result.deviceHint ? String(result.deviceHint).slice(0, 240) : ""
  }
  return next
}

// One sentence, ending in exactly one stop: TorBox's own details already end in a period.
function deviceSentence(text) {
  var t = String(text || "").replace(/\s+$/, "")
  return t === "" || /[.!?…]$/.test(t) ? t : t + "."
}

function deviceStatusText(sign) {
  var s = sign || ({ state: "idle" })
  var mins = Math.max(1, Math.ceil((Number(s.left) || 0) / 60))
  switch (String(s.state)) {
    case "starting": return "Asking TorBox for a code…"
    case "waiting": return s.retrying ? "TorBox did not answer; still waiting (about " + mins + " min left)."
                                      : "Waiting for you to approve it at TorBox (about " + mins + " min left)."
    case "granted": return "Approved. Checking the token with TorBox…"
    case "saved": return "Signed in to TorBox."
    case "expired": return "The code expired before it was approved. Try again for a new one."
    case "denied": return s.error ? deviceSentence(s.error) : "The sign-in was declined or cancelled at TorBox."
    case "failed": return (s.error ? deviceSentence(s.error) : "The sign-in failed; see `omarchy-anime trace last`.")
                          + (s.hint ? " " + deviceSentence(s.hint) : "")
    case "cancelled": return "Cancelled."
    default: return ""
  }
}

// A Torznab list (the `accounts` answer's `torznab` block) the sheet may offer to add to: usable, empty or not. A
// refused file, or one with an entry it could not read, may show no row either, but `torznab add` would refuse to
// write it, so nothing is offered there. Service.qml asks `torznab find-local` on an open of Accounts only when this
// holds, and Browse.qml shows the offers not already listed (BrowseModel.js `torznabOffers`) only when it holds.
function torznabListUsable(block) {
  if (!block || typeof block !== "object" || isList(block)) return false
  if (block.problem) return false
  if (isList(block.skipped) && block.skipped.length > 0) return false
  return isList(block.servers)
}

// Whether `value` is a list. On Qt 6.12 an array that crossed a QML boundary reaches JS as a sequence
// (`[object V4Sequence]`): a Repeater's `modelData` and every array inside it, a `signal(var)` argument, a `list<>`
// property, and any `property var` such a value was stored in. It has `length`, indexes and Array's methods, and
// `instanceof Array` is true for it, but `Array.isArray` is false, which emptied the Settings language menus. A
// string, or an object that merely has a `length` key, is not a list.
// BrowseModel.js and ServiceLogic.js each carry this pair, word for word (a node test holds them equal).
function isList(value) {
  return Array.isArray(value) || value instanceof Array
}

// A real Array of a list's items, or [] for anything that is not a list: a defensive copy, so code that keeps or
// changes the result never holds the engine's sequence.
function listCopy(value) {
  return isList(value) ? Array.prototype.slice.call(value) : []
}

// `core.as_bool`'s twin, for every boolean setting the QML reads. `status` types them, but
// the Service's copy right after a write holds what was written, `setSetting` takes any JSON, and the bar falls back
// to the raw shell.json entry before the first `status`: a stored "true" must not read as false there either.
function asBool(value, fallback) {
  if (value === true || value === false) return value
  if (value === null || value === undefined) return fallback === true
  if (typeof value === "string") {
    var v = value.trim().toLowerCase()
    if (v === "true" || v === "1" || v === "yes" || v === "on") return true
    if (v === "false" || v === "0" || v === "no" || v === "off" || v === "") return false
    return fallback === true
  }
  return !!value
}

// The entry already holds this value for `key`, so there is nothing to write. The host's
// `updateEntryInline` returns false both when nothing changed and when it found no entry to write, so the Service
// must not ask it to write a value that is already there: that false reads as a failed save.
function settingUnchanged(entry, key, value) {
  if (!entry || typeof entry !== "object" || !Object.prototype.hasOwnProperty.call(entry, key)) return false
  return JSON.stringify(entry[key]) === JSON.stringify(value)
}

// How Panel.qml cuts the bar title (a `TextMetrics`): at `maxTitleWidth` pixels, or not at all for 0. Measured
// on Qt 6.11: `Text.ElideRight` with an `elideWidth` of 0 gives "", so no limit is `elide: false`, never a width of 0.
// A value that is not a number of pixels is `fallback` (the manifest's default).
function titleElide(maxTitleWidth, fallback) {
  var n = Number(maxTitleWidth)
  if (maxTitleWidth === null || maxTitleWidth === undefined || maxTitleWidth === "" || !isFinite(n) || n < 0)
    n = Number(fallback) || 0
  n = Math.floor(n)
  return { elide: n > 0, width: n }
}

// ------------------------------------------------------------------ AI agent prompts
// Settings' Indexers section hands the viewer's own coding agent a ready prompt: build an indexer add-on with them,
// or set up Prowlarr or Jackett. The prompts are the CLI's (`addons prompt --json`, read once, with this computer's
// command and add-on folder filled in), and Omarchy's `omarchy-agent --pick --prompt` starts the default agent with
// one, or opens Omarchy's agent chooser when none is set (the prompt is not kept then: the viewer presses again).
var AGENT_KINDS = ["build", "prowlarr"]
// Two clicks on the same button this close together are one: the second would open a second agent window.
var AGENT_REPEAT_MS = 3000
// Handed over, which is all the launch knows: with no default agent, `--pick` opens Omarchy's chooser and drops the
// prompt, and an agent that is not installed opens nothing (its exit is logged, not seen here).
var AGENT_OPENED = "Handed to your default AI agent: read what it writes before you say yes. If Omarchy's agent "
                   + "chooser opened instead, choose one and press again; if nothing opened, run omarchy agent in a "
                   + "terminal to see why."
var AGENT_READING = "Reading the prompt…"
var AGENT_AGAIN = "Already opening it."
var AGENT_NO_CLI = "The plugin's command could not be found, so there is no prompt to hand over."

function agentPromptArgv(bin, trace) {
  return [String(bin), "--trace", String(trace), "addons", "prompt", "--json"]
}

// `addons prompt --json`'s answer: `{prompts: {build, prowlarr}}`, or `{prompts: null, error}` when it is not that.
function agentPromptsFrom(text) {
  var r = null
  try { r = JSON.parse(String(text || "")) } catch (e) { r = null }
  var got = r && typeof r === "object" && r.prompts && typeof r.prompts === "object" ? r.prompts : null
  var prompts = {}
  for (var i = 0; i < AGENT_KINDS.length; i++) {
    var p = got ? got[AGENT_KINDS[i]] : null
    if (typeof p !== "string" || p.trim() === "")
      return { prompts: null, error: r && r.error ? String(r.error) : "the plugin's command gave no prompt" }
    prompts[AGENT_KINDS[i]] = p
  }
  return { prompts: prompts, error: "" }
}

// The row's line when the prompts could not be read.
function agentPromptsFailed(error) {
  return "The prompt could not be read (" + String(error || "no answer") + "). `omarchy-anime trace last` says why."
}

// `omarchy-agent` does not return once the agent is up: `omarchy-launch-tui` execs `uwsm-app`, which execs
// `systemd-run --user --scope` with the terminal in the foreground, so it ends when the agent's window closes. A
// Process would hold it all that time, and a shell reload would close the window with it. So it runs detached,
// through this wrapper: the arguments after the first four (the command, the trace, the kind, the prompt's length)
// are the exact command. It reads the default agent's name first (`omarchy-default-agent` with no argument only reads
// its file; empty means `--pick` opens Omarchy's chooser and drops the prompt), logs `ui agent.launch` with it in the
// background, so the agent is not kept waiting, and once the command ends logs `ui agent.exit` (its code and how long
// it ran) with it, both under the click's trace. The name is cut to the characters an agent's name has, so it cannot
// break the line's JSON.
var AGENT_SCRIPT = [
  "s=$(date +%s); b=$1; t=$2; k=$3; n=$4; shift 4;",
  "a=$(omarchy-default-agent 2>/dev/null | tr -cd 'A-Za-z0-9._-'); w=false; [ -n \"$a\" ] || w=true;",
  "\"$b\" --trace \"$t\" log-event ui agent.launch --data",
  "\"$(printf '{\"kind\":\"%s\",\"agent\":\"%s\",\"chooser\":%s,\"chars\":%d}' \"$k\" \"$a\" \"$w\" \"$n\")\"",
  ">/dev/null 2>&1 &",
  "\"$@\"; c=$?; r=PASS; l=info; [ \"$c\" -eq 0 ] || { r=FAIL; l=warn; };",
  "\"$b\" --trace \"$t\" log-event ui agent.exit --result \"$r\" --level \"$l\" --data",
  "\"$(printf '{\"kind\":\"%s\",\"agent\":\"%s\",\"chooser\":%s,\"code\":%d,\"seconds\":%d}'",
  "\"$k\" \"$a\" \"$w\" \"$c\"",
  "\"$(($(date +%s) - s))\")\""
].join(" ")

// The detached launch for one button, or [] when there is nothing to launch (an unknown kind, no prompt, no command).
function agentArgv(bin, trace, kind, prompt) {
  if (!bin || AGENT_KINDS.indexOf(String(kind)) < 0 || typeof prompt !== "string" || prompt.trim() === "") return []
  return ["sh", "-c", AGENT_SCRIPT, "omaanime-agent", String(bin), String(trace), String(kind), String(prompt.length),
          "omarchy-agent", "--pick", "--prompt", prompt]
}

// Why a click is not launched: "" to launch, or the row's line.
function agentRefusal(lastLaunchMs, nowMs) {
  var last = Number(lastLaunchMs)
  return last > 0 && Number(nowMs) - last >= 0 && Number(nowMs) - last < AGENT_REPEAT_MS ? AGENT_AGAIN : ""
}

// A copy of `map` with `key` set to `value`.
function withKey(map, key, value) {
  var next = {}
  for (var k in (map || {})) next[k] = map[k]
  next[String(key)] = value
  return next
}

// The mpv socket the Service connects to: `$XDG_RUNTIME_DIR/omaanime-mpv.sock` (the launcher's and the CLI's), else
// the path `status` reports (`core.MPV_SOCKET`: `/run/user/<uid>/…` when the variable is unset), else "" (no socket
// until a status answer names one). Never a world-writable folder such as `/tmp`, where another user could listen.
function socketPathFor(runtimeDir, reported) {
  var rt = String(runtimeDir || "")
  if (rt.charAt(0) === "/") return rt.replace(/\/+$/, "") + "/omaanime-mpv.sock"
  var cli = String(reported || "")
  return /^\/run\/user\/[0-9]+\/omaanime-mpv\.sock$/.test(cli) ? cli : ""
}
