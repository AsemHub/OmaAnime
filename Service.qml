import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Hyprland
import "ServiceLogic.js" as Logic

// Headless service for the Anime plugin.
//
// Division of labour: all decisions live in the Python CLI (bin/omarchy-anime). This object only
//   1. keeps one JSON IPC socket to mpv for live playback state (pause, position, eof),
//   2. polls `omarchy-anime status` for what the CLI knows (Hyprland window, now-playing, settings),
//   3. calls `omarchy-anime tick` on a cadence so the resume position and the watched rule run in Python,
//   4. exposes state properties for Panel.qml and an IpcHandler ("omaanime") for keybinds.
//
// Durable state (mpv, the Hyprland window, now-playing.json, resume.json) lives outside this process:
// the shell tears this object down and rebuilds it on every file save under the plugin directory.
// Nothing here blocks: every CLI call is a Process or execDetached.
Item {
  id: root
  visible: false
  width: 0
  height: 0

  // Injected by the shell (shell.qml ensureService()).
  property var shell: null
  property var manifest: null
  property var pluginRegistry: null

  readonly property string pluginId: manifest && manifest.id ? String(manifest.id) : "io.github.asemhub.omaanime"
  // Omarchy 4.0.3 strips `__sourceDir` from the manifest it hands third-party services
  // (shell.qml publicPluginManifest), so the plugin folder comes from this file's own URL.
  readonly property string sourceDir: {
    if (manifest && manifest.__sourceDir) return String(manifest.__sourceDir)
    var u = String(Qt.resolvedUrl("."))
    if (u.indexOf("file://") === 0) u = u.substring(7)
    return u.replace(/\/$/, "")
  }
  readonly property string bin: sourceDir ? sourceDir + "/bin/omarchy-anime" : ""
  // The CLI's own socket path, from `status` (`socketPath`): the one to use when the shell has no XDG_RUNTIME_DIR.
  property string reportedSocketPath: ""
  readonly property string socketPath: Logic.socketPathFor(Quickshell.env("XDG_RUNTIME_DIR"), reportedSocketPath)

  // ------------------------------------------------------------------ state
  // The live mpv socket, or null between reconnect attempts (it is rebuilt per attempt).
  readonly property var mpv: mpvLoader.item
  readonly property bool socketConnected: mpv !== null && mpv !== undefined && mpv.connected === true
  // From the mpv socket (live):
  property bool running: false          // socket connected
  property bool paused: false
  property bool idle: true              // idle-active: nothing loaded
  property bool eof: false
  // What auto-next last started, so the ten-second tick at EOF cannot fire the same
  // advance again while the play it already started is failing. Cleared whenever a play begins.
  property var lastAdvance: null
  property bool cacheStalled: false     // paused-for-cache
  property real timePos: 0
  property real duration: 0
  property string mediaTitle: ""
  // A side-loaded track whose offset could not be estimated loads unshifted, and mpv's own
  // z/Z move this 0.1 s per press — and only while the mpv window has keyboard focus. Observed, so
  // the popout shows the live value without polling.
  property real subDelay: 0
  // From `omarchy-anime status` (polled):
  property bool windowVisible: false
  property bool parked: false
  property string subLang: ""
  property var nowPlaying: null         // now-playing.json or null
  property var resume: null
  property var settings: ({})          // the effective merge: defaults < config.json < shell.json
  property var settingsSource: ({})    // per key: "default" | "config" | "shell"
  property var settingsEntry: ({})     // the RAW shell.json entry; what a write has to preserve
  property var settingsReadOnly: ({})  // kept for one release; `settingsAdvanced` replaced it
  property var languageNames: ({})     // code -> English name, the sheet's language labels
  // Why the last settings write failed, "" after one succeeds. Not `lastError`: that is the play's
  // error, which the popout and the overlay show with "Enter retries" under it until the next play.
  property string settingsError: ""
  // Accounts. Three providers, from `omarchy-anime accounts` — a state, a plan and at
  // most the last four characters of each token. **Never the token**: the CLI does not print it and
  // this side never reads the file.
  property var accounts: ({})
  property bool accountsLoading: false
  property string accountsError: ""
  property string configPath: ""
  // From `omarchy-anime watching` (polled slowly):
  property var watching: []
  property bool watchingLoaded: false
  // Why the last `watching` read did not return a list (`Logic.listFailure`), or
  // null. Kept until the next answer replaces it, so the five-minute re-read does not flicker.
  property var watchingFailure: null
  // The rows on show are the saved list AniList's outage answered with: the CLI's `stale` marker
  // (`Logic.listAnswerStale`), or null. The first fresh answer clears it, and with it the line that says so.
  property var watchingSaved: null
  // A re-read asked for while one is running (an account change, a flushed write) is
  // queued here, not dropped; the running read's answer is then superseded and `onExited` re-runs.
  property bool watchingStale: false
  // An AniList sign-in succeeded or a sign-out ran. Browse drops every Lists status it
  // holds and re-reads the one showing; this Service re-reads Continue Watching.
  signal anilistAccountChanged()
  // The line an empty Continue Watching draws is `Logic.listStateText` over `watchingLoaded` and `watchingFailure`,
  // asked by the popout and the overlay each for itself: each says where Settings is from the surface drawing it.
  // Transient UI state:
  // The raw `status.play` entry, kept apart from `busy` (its one-line rendering)
  // so Panel.qml can ask `Logic.progressRows` for every stage the play has passed, not just the
  // sentence for whichever one is current. Null whenever the poll has nothing running.
  //
  // Named `playEntry`, NOT `play`. A QML property and a function of the same name on the
  // same object collide and the property wins, so `property var play` silently turned every
  // `service.play(...)` call — the Browse grid, `playNext`, the keybind, the IPC handler — into
  // "Property 'play' is not a function". `qmllint` exits 0 on it. A test
  // is the guard; do not rename this back.
  property var playEntry: null
  property string busy: ""              // "Finding a release…" while a play runs, else ""
  // A play that stopped waiting for TorBox and holds the viewer's choices: its plain sentence, never an error
  // (`Logic.playNotice`'s `held`); "" otherwise. The choices themselves are on `playEntry.choice`.
  property string playHeld: ""
  // "Tell me when it's ready" recorded for the shared entry's episode: its acknowledgement, for the popout
  // (`Logic.playNotice`'s `later`); "" otherwise.
  property string playLater: ""
  // What `later` answered for the last "Tell me when it's ready" this Service ran (`Logic.laterAnswer`, with the
  // click's trace), so Browse says that, not its own guess; null before any.
  property var laterAnswer: null
  property string lastError: ""
  property string lastTrace: ""
  // The CLI's error JSON kept in pieces instead of flattened into one string, so the UI can say
  // which stage failed, what each provider answered and how to retry.
  property string lastErrorStage: ""
  property string lastErrorSuggest: ""
  property var lastErrorCandidates: []
  property string lastErrorTrace: ""
  // The sentence a person reads, kept apart from `lastError` (the record). Set
  // from `Logic.errorSentences` on both failure paths below — the poll's `Logic.playNotice` (a play
  // adopted from `status.play`) and `playProc`'s own JSON (a play this Service started itself) —
  // falling back to the old string-split reading for a response written before `say` shipped.
  // `headline` is always filled while `lastError` is; `action` may legitimately be "" (Panel.qml's
  // `if (action !== "")` exists for exactly that). The one exception is "play returned no JSON"
  // below, which never had a `say` to read in the first place and leaves all three cleared, for
  // Panel.qml's `errorHeadline()`/`errorAction()` to render instead.
  // Whether the failure is one the release picker can answer (`Logic.wantsPicker`); the
  // detail line then names `p`, and the overlay opens the picker by itself.
  property bool lastErrorPicker: false
  // The episode the running (or last) play was for. The picker a refusal opens needs it, and the
  // CLI's error JSON does not carry it.
  property int playMediaId: 0
  property int playEpisode: 0
  // Why the last play asked for did not start: one was already starting ("" once that play ends), or the entry is not
  // out yet (`refusalClear` ends it, or a play started after it).
  property string playRefusal: ""
  // The play entry's trace when a play was refused for an entry that is not out ("" for none): a play running beside
  // the refusal does not clear it, one started after it does.
  property string refusalBeside: ""
  // The `releases` JSON for the picker, and whether a search for it is running.
  property var pickerData: null
  property bool pickerLoading: false
  property string pickerError: ""
  signal pickerWanted(int mediaId, int episode)
  // Set by auto-next just before its play, so a refusal there is told to the player.
  property bool pendingAutoNext: false
  property bool playIsAutoNext: false
  property string lastErrorHeadline: ""
  property string lastErrorAction: ""
  property string lastErrorHint: ""
  // Standing notices rather than errors: the AniList token expires within 14 days, or the
  // AniList request budget is nearly spent (the 90 % step). Kept apart from lastError so a play
  // failure and a standing notice cannot overwrite each other; `notice` is what the UI shows.
  property string tokenWarning: ""
  property string rateWarning: ""
  // The mpv socket has been down for over a minute while mpv itself is running. First in
  // the chain on purpose — it describes a degradation happening right now and names the one command
  // that ends it, where the other two are standing notices that will still be true tomorrow.
  property string socketWarning: ""
  // The player process is older than the script it loaded at startup, so it is running the
  // previous copy of it. Second in the chain, for the same reason the socket notice is first: it is
  // wrong right now and one command fixes it.
  property string staleWarning: ""
  property bool staleWarned: false
  // The trace of the last play failure this service has already shown. A failure stays in
  // `play-status.json` until the next play, so without this the 10 s poll would resurrect an error
  // the user has read and dismissed, every ten seconds, forever.
  property string playSeenTrace: ""
  readonly property string notice: socketWarning !== "" ? socketWarning
                                 : (staleWarning !== "" ? staleWarning
                                 : (tokenWarning !== "" ? tokenWarning : rateWarning))

  readonly property bool playing: running && !idle && !paused
  readonly property bool hasMedia: running && !idle && nowPlaying !== null
  readonly property real percent: duration > 0 ? Math.max(0, Math.min(1, timePos / duration)) : 0
  readonly property int pollIntervalSec: Number(settings && settings.pollIntervalSec) || 5
  readonly property bool autoNext: Logic.asBool(settings ? settings.autoNext : undefined, false)

  // ------------------------------------------------------------------ trace
  // Every user action mints a trace id; the CLI logs under it, so `omarchy-anime trace last`
  // shows click → resolve → mpv in one timeline.
  function newTrace(action) {
    return "ui-" + action + "-" + Math.floor(Date.now() / 1000) + "-" + Math.floor(Math.random() * 65536).toString(16)
  }

  function logEvent(trace, stage, data) {
    if (!bin) return
    Quickshell.execDetached(Logic.logEventLaunch(bin, trace, "ui", stage, data))
  }

  // The service's own lifecycle, under component "service" in the same JSONL the CLI writes, and
  // under one trace id per outage so `omarchy-anime trace <id>` shows down -> restored together.
  // A dead socket once disabled every progress write unnoticed, precisely because nothing in this
  // file ever wrote a line.
  function logService(stage, data, level) {
    if (!bin) return
    Quickshell.execDetached(Logic.logEventLaunch(bin, socketTrace || newTrace("socket"), "service", stage, data, level))
  }

  // ---------------------------------------------------------------- actions
  function run(action, args) {
    if (!bin) return ""
    var trace = newTrace(action)
    lastTrace = trace
    logEvent(trace, "click", { action: action, args: args })
    Quickshell.execDetached(Logic.cliLaunch([bin, "--trace", trace], args))
    if (Logic.socketExpects(args)) expectPlayer(action)
    settle.restart()
    return trace
  }

  function toggleWindow() { run("toggleWindow", ["player", "toggle"]) }
  function showWindow() { run("showWindow", ["player", "show"]) }
  function hideWindow() { run("hideWindow", ["player", "hide"]) }
  function playPause() {
    if (!running || idle) return
    // Straight to mpv: no fork, and the observed `pause` property confirms it.
    mpvSend(["cycle", "pause"], 0)
  }
  function stop() { run("stop", ["player", "stop"]) }

  // ---------------------------------------------------------------------- settings
  // Omarchy 4.0.3 has no settings form: `barWidget.schema` is copied into registry metadata and
  // never read (one assignment at shell.qml:1408, no reader of `defaultValue`, `Ui/MultiSelect.qml`
  // instantiated by nothing). The plugin therefore owns its own sheet, and this is its write path.
  //
  // `shell.updateEntryInline(id, settings)` **replaces** our shell.json entry with `{id, ...settings}`
  // (shell.qml:1092-1093), so every key the entry already carries is copied forward first — the same
  // idiom plugins/panels/clock/BarWidget.qml uses. And only a key the user actually changed is
  // written: putting the merged values in would materialise all thirteen keys into shell.json and
  // silently shadow the user's config.json for every one of them.
  function settingsCopy(without) {
    var out = ({})
    var cur = settingsEntry || ({})
    for (var k in cur) if (k !== "id" && k !== without) out[k] = cur[k]
    return out
  }

  function writeEntry(action, entry, key, value) {
    if (!shell || typeof shell.updateEntryInline !== "function") {
      settingsError = "This Omarchy build does not let a plugin write its own settings"
      return false
    }
    var trace = newTrace("setting")
    var ok = shell.updateEntryInline(pluginId, entry) === true
    // What was written, which keys survived the wholesale replace, and what the host said.
    logEvent(trace, "settings." + action, { key: key, value: value === undefined ? null : value,
                                            keys: Object.keys(entry), ok: ok })
    if (!ok) {
      settingsError = "Could not save " + key + " — see `omarchy-anime trace " + trace + "`"
      return false
    }
    settingsError = ""
    // Optimistic local state so the control does not flicker back; `refresh()` is the read-back
    // and `status` is the source of truth a moment later — but only once a `status` run
    // that *started after this write* has come back.
    settingsWrittenAt = Date.now()
    settingsEntry = entry
    var merged = ({})
    for (var mk in settings) merged[mk] = settings[mk]
    var src = ({})
    for (var sk in settingsSource) src[sk] = settingsSource[sk]
    if (value === undefined) {
      delete merged[key]                       // back to config.json / the default; status re-fills it
      src[key] = "default"
    } else {
      merged[key] = value
      src[key] = "shell"
    }
    settings = merged
    settingsSource = src
    settle.restart()
    return true
  }

  // Write one setting into our own shell.json entry.
  function updateSetting(key, value) {
    // A value the entry already holds is saved, not "Could not save": the host answers false both
    // when nothing changed and when the write failed, so it is not asked. A stored "true" against `true` still writes.
    if (Logic.settingUnchanged(settingsEntry, String(key), value)) {
      logEvent(newTrace("setting"), "settings.write", { key: String(key), value: value, ok: true, unchanged: true })
      settingsError = ""
      return true
    }
    var entry = settingsCopy()
    entry[String(key)] = value
    return writeEntry("write", entry, String(key), value)
  }

  // Drop one setting from the entry, handing the key back to config.json (or to the default).
  function resetSetting(key) {
    if (!(settingsEntry && settingsEntry.hasOwnProperty(String(key)))) {
      settingsError = ""                     // nothing to reset succeeds, like an unchanged write
      return true
    }
    return writeEntry("reset", settingsCopy(String(key)), String(key), undefined)
  }

  // Drop several at once — **one** write, not one per key. Ten sequential writes would each copy
  // the entry forward from `settingsEntry`, which is exactly the interleaving that resurrects a removed key;
  // a batch has no interleaving to get wrong. Used by the sheet's "Advanced: reset all".
  function resetSettings(keys) {
    if (!Logic.isList(keys) || keys.length === 0) return true
    var drop = ({})
    var i
    for (i = 0; i < keys.length; i++) drop[String(keys[i])] = true
    var entry = ({})
    var cur = settingsEntry || ({})
    var removed = []
    for (var k in cur) {
      if (k === "id") continue
      if (drop[k]) { removed.push(k); continue }
      entry[k] = cur[k]
    }
    if (removed.length === 0) {
      settingsError = ""                     // as `resetSetting`'s nothing-to-reset
      return true
    }
    return writeEntry("resetMany", entry, removed.join(","), undefined)
  }

  // Subtitle timing nudge. Straight down the existing socket like playPause(): `add` is
  // atomic on mpv's side, so two fast clicks cannot race a stale read, and the observed `sub-delay`
  // property is the read-back. Verified over IPC on mpv 0.41: `add sub-delay 1.0` answers
  // success and `get_property sub-delay` reports 1.0.
  // Note the property survives `loadfile` (`--reset-on-next-file` resets nothing by default); the
  // CLI resets it to 0 on every play, so a nudge only lives as long as the episode it was made for.
  function nudgeSubDelay(delta) {
    if (!running || idle) return
    mpvSend(["add", "sub-delay", Number(delta)], 0)
    logEvent(newTrace("subDelay"), "click", { action: "nudgeSubDelay", delta: Number(delta) })
  }

  function resetSubDelay() {
    if (!running || idle) return
    mpvSend(["set_property", "sub-delay", 0], 0)
    logEvent(newTrace("subDelay"), "click", { action: "resetSubDelay" })
  }
  function startPlayer() { run("startPlayer", ["player", "start"]) }

  // ---------------------------------------------------------------------------- accounts

  // A read asked for while one runs is queued, not dropped: the running one may have read the Torznab
  // servers before a write it must show, so it runs again once it ends.
  property bool accountsQueued: false
  property bool accountsQueuedRefresh: false

  function loadAccounts(refresh) {
    if (!bin) return
    if (accountsProc.running) {
      accountsQueued = true
      accountsQueuedRefresh = accountsQueuedRefresh || !!refresh
      return
    }
    accountsLoading = true
    accountsError = ""
    accountsProc.command = refresh ? [bin, "accounts", "--refresh"] : [bin, "accounts"]
    accountsProc.running = true
  }

  Process {
    id: accountsProc
    stdout: StdioCollector {
      onStreamFinished: {
        root.accountsLoading = false
        // The open's request is used by this answer, whatever it is, so a later read-back (after an
        // add, check or remove) never asks for a local server.
        var findWanted = root.torznabFindWanted
        root.torznabFindWanted = false
        var a
        try {
          a = JSON.parse(this.text)
        } catch (e) {
          root.accountsError = "could not read the account state"
          return
        }
        if (a && typeof a === "object" && a.ok === false) {
          root.accountsError = String(a.error || "could not read the account state")
          return
        }
        if (a && typeof a === "object") root.accounts = a
        // An open of the sheet asked for it, and servers can be added to the Torznab list: look for a local one.
        if (findWanted && a && typeof a === "object" && Logic.torznabListUsable(a.torznab)) root.findLocalTorznab()
      }
    }
    onExited: function (code) {
      if (code !== 0) root.accountsLoading = false
      if (root.accountsQueued) {
        var refresh = root.accountsQueuedRefresh
        root.accountsQueued = false
        root.accountsQueuedRefresh = false
        Qt.callLater(function () { root.loadAccounts(refresh) })
      }
    }
  }

  // **The token goes over stdin, never argv** — the same idiom the shell's own network panel uses
  // for a wifi passphrase (`plugins/panels/network/Panel.qml:776-785`), and the reason `auth`
  // refuses a `--token <value>` at all. `stdinEnabled` defaults to false and `write()`
  // does nothing while it is; setting it false straight after the write is what gives the CLI its
  // EOF (verified against the installed Quickshell 0.3.1 docs).
  // `true` when the token was taken; otherwise why not, "busy" or "no_cli", so Browse says
  // "another sign-in is being checked" only when one is. Browse keeps a refused token in its field either way.
  function signIn(provider, secret) {
    if (!bin) {
      logEvent(newTrace("auth"), "ui.accounts.signin", { provider: String(provider), state: "refused",
                                                          reason: "no_cli" })
      accountsError = "the plugin's command was not found"
      return "no_cli"
    }
    if (authProc.running) {
      // One check at a time.
      logEvent(newTrace("auth"), "ui.accounts.signin", { provider: String(provider), state: "refused",
                                                          reason: "busy", checking: authProc.provider })
      return "busy"
    }
    var trace = newTrace("auth")
    logEvent(trace, "ui.accounts.signin", { provider: String(provider) })   // never the secret
    authProc.provider = String(provider)
    authProc.secret = String(secret)
    Logic.cliOn(authProc, [bin, "--trace", trace, "auth"], [provider, "--token", "-"])
    authProc.running = true
    return true
  }

  Process {
    id: authProc
    property string provider: ""
    property string secret: ""
    stdinEnabled: true
    onStarted: {
      write(secret + "\n")
      secret = ""                 // gone from this object the moment it is handed over
      stdinEnabled = false        // the CLI's EOF
    }
    stdout: StdioCollector {
      onStreamFinished: {
        var r
        try {
          r = JSON.parse(this.text)
        } catch (e) {
          root.accountsError = "sign-in failed; see `omarchy-anime trace last`"
          return
        }
        if (r && r.ok === false) {
          root.accountsError = String(r.error || "the provider did not accept that token")
          return
        }
        root.accountsError = ""
        root.loadAccounts(true)     // the read-back is what the row draws
        // A new AniList sign-in ends "Sign in to AniList" now, not at the next five-minute read.
        if (authProc.provider === "anilist") root.noteAnilistAccountChanged("signin")
      }
    }
    onExited: function (code) {
      authProc.secret = ""
      if (code !== 0 && root.accountsError === "")
        root.accountsError = "sign-in failed; see `omarchy-anime trace last`"
    }
  }

  // TorBox sign-in with a device code, the default way in. The CLI shows a 6-digit code,
  // waits (up to 10 minutes) for the user to approve it at TorBox, and saves the token through the
  // same verify-write-read-back path as a paste. Its progress arrives on stderr as
  // `{"event":"device",...}` lines (Logic.deviceEvent picks them out of the log lines) and its
  // result on stdout. No stdin: nothing secret goes in, and neither the device code nor the token
  // ever comes out.
  property var torboxDevice: ({ state: "idle" })
  // The search box's cancellation pattern (Browse.qml, `searchExpectedStop`): exit and end of stdout
  // arrive in no guaranteed order, so a run is over only when both have been seen, and output that
  // arrives while `deviceExpectedStop` is set belongs to the run being stopped, not to the sheet.
  property bool deviceExpectedStop: false
  // The code is put on the clipboard the moment it shows (`Logic.deviceCopyWanted`), so the viewer pastes it
  // at TorBox. "copied" or "failed" is what the sheet says under the code; "" before the copy has answered.
  property string torboxCodeClip: ""
  property string deviceCopiedTrace: ""
  property bool deviceQueued: false
  property bool deviceExited: true
  property bool deviceStreamDone: true

  function signInTorBoxDevice() {
    var decision = Logic.deviceStartDecision({ bin: !!bin, running: deviceProc.running, exited: deviceExited,
                                               streamDone: deviceStreamDone, expectedStop: deviceExpectedStop,
                                               state: torboxDevice.state })
    if (decision === "start") {
      startTorBoxDevice()
      return true
    }
    logEvent(deviceProc.trace || newTrace("auth"), "ui.accounts.device",
             { provider: "torbox", state: decision === "queue" ? "queued" : "refused", reason: decision,
               running: deviceProc.running, exited: deviceExited, streamDone: deviceStreamDone })
    if (decision !== "queue") return false
    // Started from deviceRunEnded() once the ending run is fully gone.
    deviceQueued = true
    deviceExpectedStop = true
    torboxDevice = { state: "starting" }
    if (deviceProc.running) deviceProc.running = false
    return true
  }

  function startTorBoxDevice() {
    var trace = newTrace("auth")
    logEvent(trace, "ui.accounts.device", { provider: "torbox", state: "starting" })
    accountsError = ""
    deviceQueued = false
    deviceExpectedStop = false
    deviceExited = false
    deviceStreamDone = false
    torboxDevice = { state: "starting" }
    torboxCodeClip = ""
    deviceProc.trace = trace
    deviceProc.command = [bin, "--trace", trace, "auth", "torbox", "--device"]
    deviceProc.running = true
  }

  function deviceRunEnded() {
    if (!deviceExited || !deviceStreamDone) return
    deviceEndTimer.stop()
    if (deviceQueued) Qt.callLater(startTorBoxDevice)
  }

  function cancelTorBoxDevice() {
    deviceQueued = false
    if (Logic.deviceIsTerminal(torboxDevice.state) || torboxDevice.state === "idle") {
      // Closing a finished panel: a run that has not fully ended keeps its last words to itself.
      if (deviceProc.running || !deviceExited || !deviceStreamDone) deviceExpectedStop = true
      torboxDevice = { state: "idle" }
      return
    }
    logEvent(deviceProc.trace || newTrace("auth"), "ui.accounts.device", { provider: "torbox", state: "cancelled" })
    torboxDevice = { state: "cancelled" }
    if (deviceProc.running) {
      deviceExpectedStop = true
      deviceProc.running = false
    }
  }

  function setTorBoxDevice(next) {
    var was = torboxDevice.state
    torboxDevice = next
    if (Logic.deviceCopyWanted(next, deviceProc.trace, deviceCopiedTrace)) copyDeviceCode(next.code)
    if (next.state === was) return
    // The state only: never the code, the URL's query, or a line of the CLI's output.
    logEvent(deviceProc.trace || newTrace("auth"), "ui.accounts.device", { provider: "torbox", state: String(next.state) })
    if (next.state === "saved") {
      accountsError = ""
      loadAccounts(true)          // the read-back is what the row draws
    }
  }

  // **The code goes over stdin, never argv** (the token's idiom, `signIn`): `wl-copy` with no text argument copies
  // what it reads until EOF, exactly, so no newline is written. `wl-copy`, not `Quickshell.clipboardText`: that one is
  // documented to work on Wayland only while a Quickshell window is focused, and the viewer is often in the browser by
  // the time TorBox answers. The log says that it was copied, never the code.
  function copyDeviceCode(code) {
    deviceCopiedTrace = deviceProc.trace
    torboxCodeClip = ""
    if (deviceCopyProc.running) deviceCopyProc.running = false
    deviceCopyProc.secret = String(code)
    deviceCopyProc.trace = deviceProc.trace
    deviceCopyProc.running = true
  }

  // `wl-copy`'s exit: what the row says (`Logic.deviceClipResult`), and one line that says it, never the code.
  function deviceCopyEnded(code) {
    torboxCodeClip = Logic.deviceClipResult(code)
    logEvent(deviceCopyProc.trace || newTrace("auth"), "ui.accounts.device",
             { provider: "torbox", clipboard: torboxCodeClip, exit: code })
  }

  Process {
    id: deviceCopyProc
    property string secret: ""
    property string trace: ""
    command: ["wl-copy"]
    stdinEnabled: true
    onStarted: {
      write(secret)
      secret = ""                 // gone from this object the moment it is handed over
      stdinEnabled = false        // wl-copy's EOF
    }
    onExited: function (code) {
      deviceCopyProc.secret = ""
      stdinEnabled = true
      root.deviceCopyEnded(code)
    }
  }

  Process {
    id: deviceProc
    property string trace: ""
    stderr: SplitParser {
      splitMarker: "\n"
      onRead: function (line) {
        if (root.deviceExpectedStop) return          // a stopped run's progress is not the sheet's
        var ev = Logic.deviceEvent(line)
        if (ev) root.setTorBoxDevice(Logic.deviceReduce(root.torboxDevice, ev))
      }
    }
    stdout: StdioCollector {
      onStreamFinished: {
        var r = null
        try { r = JSON.parse(this.text) } catch (e) { r = null }
        root.deviceStreamDone = true
        if (!root.deviceExpectedStop) {
          root.setTorBoxDevice(Logic.deviceFinish(root.torboxDevice, r))
        } else if (r && r.saved === true) {
          // Stopped too late: the token was already saved. The disk wins over "Cancelled.".
          root.logEvent(deviceProc.trace || root.newTrace("auth"), "ui.accounts.device",
                        { provider: "torbox", state: "saved_after_stop", queued: root.deviceQueued })
          if (root.deviceQueued) root.loadAccounts(true)
          else root.setTorBoxDevice(Logic.deviceFinish(root.torboxDevice, r))
        }
        root.deviceRunEnded()
      }
    }
    onExited: function (code) {
      root.deviceExited = true
      // The stream-finished handler owns the result; this only covers an exit with no output.
      if (!root.deviceExpectedStop && code !== 0 && !Logic.deviceIsTerminal(root.torboxDevice.state))
        root.setTorBoxDevice(Logic.deviceFinish(root.torboxDevice, null))
      if (!root.deviceStreamDone) deviceEndTimer.restart()
      root.deviceRunEnded()
    }
  }

  // If stdout's end never arrives after the exit, a queued sign-in must not wait forever for it.
  Timer {
    id: deviceEndTimer
    interval: 3000
    onTriggered: {
      if (root.deviceStreamDone) return
      root.logEvent(deviceProc.trace || root.newTrace("auth"), "ui.accounts.device",
                    { provider: "torbox", state: "stream_end_missing", queued: root.deviceQueued })
      root.deviceStreamDone = true
      root.deviceRunEnded()
    }
  }

  function noteAnilistAccountChanged(action) {
    logEvent(newTrace("account"), "ui.list.account",
             { provider: "anilist", action: String(action), watchingInFlight: watchingProc.running })
    anilistAccountChanged()
    refreshWatching()
  }

  function signOut(provider) {
    if (!bin || signOutProc.running) return false
    var trace = newTrace("signout")
    logEvent(trace, "ui.accounts.signout", { provider: String(provider) })
    signOutProc.provider = String(provider)
    Logic.cliOn(signOutProc, [bin, "--trace", trace, "signout"], [provider])
    signOutProc.running = true
    return true
  }

  Process {
    id: signOutProc
    property string provider: ""
    onExited: function () {
      root.loadAccounts(true)
      if (signOutProc.provider === "anilist") root.noteAnilistAccountChanged("signout")   // the lists go with it
    }
  }

  // ------------------------------------------------------------------------ Torznab servers
  // Add, check and remove run the CLI in a Process each, so the shell never waits on a server (a check takes up
  // to the CLI's `CHECK_BUDGET_S` against one that does not answer). The address and the key go to `torznab add --stdin` over stdin,
  // as a token does to `auth` (above): never argv, never a log event, never shell.json. What comes back holds the
  // key's last four characters at most. The rows come from `accounts` (its `torznab` block), and each is named by
  // its id there (`--id`): an address is never on a command line.
  property string torznabBusy: ""        // "add", "check:<id>", "remove:<id>", or "" when idle
  property var torznabChecks: ({})       // the last check's (or add's) answer per row (its `row` hash)
  property var torznabAnswer: null       // the last add's (or failed remove's) answer; Browse puts it in words

  function addTorznab(url, key) {
    if (!bin || torznabBusy !== "" || String(url).trim() === "") return false
    var trace = newTrace("torznab")
    logEvent(trace, "ui.accounts.torznab", { action: "add" })        // never the address or the key
    torznabBusy = "add"
    torznabAnswer = null
    torznabAddProc.secret = String(url).trim() + "\n" + String(key).trim()
    torznabAddProc.command = [bin, "--trace", trace, "torznab", "add", "--stdin"]
    torznabAddProc.running = true
    return true
  }

  function checkTorznab(id, row) {
    if (!bin || torznabBusy !== "") return false
    var trace = newTrace("torznab")
    logEvent(trace, "ui.accounts.torznab", { action: "check" })
    torznabBusy = "check:" + String(id)
    torznabCheckProc.row = String(row)
    torznabCheckProc.answered = false
    Logic.cliOn(torznabCheckProc, [bin, "--trace", trace, "torznab", "check"], ["--id", id])
    torznabCheckProc.running = true
    return true
  }

  function removeTorznab(id, row) {
    if (!bin || torznabBusy !== "") return false
    var trace = newTrace("torznab")
    logEvent(trace, "ui.accounts.torznab", { action: "remove" })
    torznabBusy = "remove:" + String(id)
    torznabAnswer = null
    torznabRemoveProc.row = String(row)
    Logic.cliOn(torznabRemoveProc, [bin, "--trace", trace, "torznab", "remove"], ["--id", id])
    torznabRemoveProc.running = true
    return true
  }

  // A Prowlarr or Jackett running on this computer, offered until it is listed (Browse filters the answer against the
  // list, BrowseModel.js `torznabOffers`). `torznab find-local` asks loopback only, with no key, in its own Process
  // (about 1 s); it saves nothing. Asked once per open of the sheet (`wantLocalTorznab`, then the next `accounts`
  // answer decides), never after an add, check or remove's read-back: the answer of the open stands until the next.
  property bool torznabFindWanted: false
  property var torznabLocal: null        // the last `find-local` answer: {found:[...], asked:[...]}

  function wantLocalTorznab() {
    torznabLocal = null
    torznabFindWanted = true
  }

  function findLocalTorznab() {
    if (!bin) return false
    if (torznabFindProc.running) {
      // The running find's answer serves this open; say so, or the trace goes quiet here.
      logEvent(newTrace("torznab"), "ui.accounts.torznab", { action: "find_local_skipped", reason: "already_running",
                                                            running: torznabFindProc.trace })
      return false
    }
    var trace = newTrace("torznab")
    logEvent(trace, "ui.accounts.torznab", { action: "find_local" })
    torznabFindProc.trace = trace
    torznabFindProc.command = [bin, "--trace", trace, "torznab", "find-local"]
    torznabFindProc.running = true
    return true
  }

  Process {
    id: torznabFindProc
    property string trace: ""
    stdout: StdioCollector {
      onStreamFinished: {
        var r = null
        try { r = JSON.parse(this.text) } catch (e) { r = null }
        root.torznabLocal = r && Logic.isList(r.found) ? r : null
      }
    }
    // A CLI that died before answering (a bad install, an import error) is on the trace, so
    // "found nothing" and "never ran" read differently. No offer is shown either way.
    onExited: function (code) {
      if (code !== 0)
        root.logEvent(torznabFindProc.trace || root.newTrace("torznab"), "ui.accounts.torznab",
                      { action: "find_local_failed", code: code })
    }
  }

  function noteTorznabCheck(row, r) {
    if (!row) return
    var next = {}
    for (var k in torznabChecks) next[k] = torznabChecks[k]
    if (r === null) delete next[row]
    else next[row] = r
    torznabChecks = next
  }

  Process {
    id: torznabAddProc
    property string secret: ""
    stdinEnabled: true
    onStarted: {
      write(secret + "\n")
      secret = ""                 // gone from this object the moment it is handed over
      stdinEnabled = false        // the CLI's EOF
    }
    stdout: StdioCollector {
      onStreamFinished: {
        var r = null
        try { r = JSON.parse(this.text) } catch (e) { r = null }
        root.torznabAnswer = r ? r : { ok: false, error: "The server could not be added; see `omarchy-anime trace last`" }
        // The add's check shows under the server's row too (it is there once the file is read back).
        if (r && r.check && r.words)
          root.noteTorznabCheck(String(r.check.row || ""), { ok: r.check.ok, transient: r.check.transient, words: r.words })
        // A saved server keeps the open's answer: the read-back lists it, so Browse stops offering it, and
        // another server found beside it is still offered.
        root.torznabBusy = ""
        root.loadAccounts(false)    // the rows are the file read back, not this answer
      }
    }
    onExited: function (code) {
      torznabAddProc.secret = ""
      stdinEnabled = true
      if (root.torznabBusy === "add" && code !== 0) {
        root.torznabBusy = ""
        if (root.torznabAnswer === null)
          root.torznabAnswer = { ok: false, error: "The server could not be added; see `omarchy-anime trace last`" }
      }
    }
  }

  // The row the check was asked for lives on the process, so its answer (or its error) reaches that row whether the
  // exit or the end of stdout arrives first (no ordering between the two).
  Process {
    id: torznabCheckProc
    property string row: ""
    property bool answered: false
    stdout: StdioCollector {
      onStreamFinished: {
        var r = null
        try { r = JSON.parse(this.text) } catch (e) { r = null }
        var servers = r && Logic.isList(r.servers) ? r.servers : []
        for (var i = 0; i < servers.length; i++) root.noteTorznabCheck(String(servers[i].row || ""), servers[i])
        if (servers.length === 0)
          root.noteTorznabCheck(torznabCheckProc.row, { ok: false, error: r && r.error ? String(r.error)
                                                                                     : "The check did not answer" })
        torznabCheckProc.answered = true
        if (root.torznabBusy.indexOf("check:") === 0) root.torznabBusy = ""
        root.loadAccounts(false)    // what the server is now remembered to be
      }
    }
    onExited: function (code) {
      if (root.torznabBusy.indexOf("check:") === 0) root.torznabBusy = ""
      if (code !== 0 && !torznabCheckProc.answered)
        root.noteTorznabCheck(torznabCheckProc.row, { ok: false, error: "The check did not answer" })
    }
  }

  Process {
    id: torznabRemoveProc
    property string row: ""
    stdout: StdioCollector {
      onStreamFinished: {
        var r = null
        try { r = JSON.parse(this.text) } catch (e) { r = null }
        // A failure, and a server that was no longer there (`removed: false`), are said; never a silent success.
        if (!r || r.ok === false || r.removed === false)
          root.torznabAnswer = r ? r : { ok: false, error: "The server could not be removed; see `omarchy-anime trace last`" }
        root.noteTorznabCheck(torznabRemoveProc.row, null)
        if (root.torznabBusy.indexOf("remove:") === 0) root.torznabBusy = ""
        root.loadAccounts(false)    // the rows are the file read back
      }
    }
    onExited: function (code) {
      if (root.torznabBusy.indexOf("remove:") === 0) root.torznabBusy = ""
      if (code !== 0) root.loadAccounts(false)
    }
  }

  // ---------------------------------------------------------------------- AI agent prompts
  // Settings' Indexers buttons. The prompts are read from the CLI once, on the first click (`agentPromptProc`), and
  // each click hands one to `omarchy-agent` detached (`Logic.agentArgv` says why detached). A click while they are
  // being read waits for them, the newest click winning; a second click on the same button just after its launch
  // is answered on its row, not dropped. The click, the read, the launch and the agent's exit are all logged.
  property var agentPrompts: null        // {build, prowlarr}, once read
  property var agentNotes: ({})          // the line under each button, by kind
  property var agentLaunched: ({})       // when each kind was last launched (ms)
  property string agentPending: ""       // the kind to launch once the prompts are read
  property string agentPendingTrace: ""

  function askAgent(kind) {
    var k = String(kind)
    if (Logic.AGENT_KINDS.indexOf(k) < 0) return false
    var trace = newTrace("agent")
    logEvent(trace, "agent.click", { kind: k, prompts: agentPrompts ? "read" : agentPromptProc.running ? "reading"
                                                                                                       : "unread" })
    if (!bin) {
      agentNotes = Logic.withKey(agentNotes, k, Logic.AGENT_NO_CLI)
      return false
    }
    if (agentPrompts) return launchAgent(k, trace)
    if (agentPending !== "" && agentPending !== k) agentNotes = Logic.withKey(agentNotes, agentPending, "")
    agentPending = k
    agentPendingTrace = trace
    agentNotes = Logic.withKey(agentNotes, k, Logic.AGENT_READING)
    if (!agentPromptProc.running) {
      agentPromptProc.trace = trace
      agentPromptProc.answer = null
      agentPromptProc.code = null
      agentPromptProc.done = false
      agentPromptProc.command = Logic.agentPromptArgv(bin, trace)
      agentPromptProc.running = true
    }
    return true
  }

  function launchAgent(kind, trace) {
    var refusal = Logic.agentRefusal(agentLaunched[kind], Date.now())
    if (refusal !== "") {
      logEvent(trace, "agent.refused", { kind: kind, why: "just launched", sinceMs: Date.now() - agentLaunched[kind] })
      agentNotes = Logic.withKey(agentNotes, kind, refusal)
      return false
    }
    var argv = Logic.agentArgv(bin, trace, kind, agentPrompts[kind])
    if (argv.length === 0) {
      logEvent(trace, "agent.refused", { kind: kind, why: "no prompt" })
      agentNotes = Logic.withKey(agentNotes, kind, Logic.agentPromptsFailed("no prompt for this button"))
      return false
    }
    // The wrapper logs `agent.launch` and `agent.exit` itself, with the default agent's name (`Logic.AGENT_SCRIPT`).
    Quickshell.execDetached(argv)
    agentLaunched = Logic.withKey(agentLaunched, kind, Date.now())
    agentNotes = Logic.withKey(agentNotes, kind, Logic.AGENT_OPENED)
    return true
  }

  // Once both ends of the read are seen, in either order (or it never started): keep the prompts and launch the
  // click that waited, or say on that click's row why there is no prompt. A failed read is tried again next click.
  function agentPromptsEnd(neverStarted) {
    var p = agentPromptProc
    if (p.done || (!neverStarted && (p.answer === null || p.code === null))) return
    p.done = true
    var read = neverStarted ? { prompts: null, error: "the plugin's command could not be started" }
                            : Logic.agentPromptsFrom(p.answer)
    logEvent(p.trace, "agent.prompts", { ok: read.prompts !== null, code: p.code, error: read.error })
    var kind = agentPending
    var trace = agentPendingTrace
    agentPending = ""
    agentPendingTrace = ""
    if (read.prompts !== null) {
      agentPrompts = read.prompts
      if (kind !== "") launchAgent(kind, trace)
    } else if (kind !== "") {
      agentNotes = Logic.withKey(agentNotes, kind, Logic.agentPromptsFailed(read.error))
    }
  }

  Process {
    id: agentPromptProc
    property string trace: ""
    property var answer: null             // its stdout, once it has ended
    property var code: null
    property bool done: false
    stdout: StdioCollector {
      onStreamFinished: {
        agentPromptProc.answer = this.text
        root.agentPromptsEnd(false)
      }
    }
    onExited: function (code) {
      agentPromptProc.code = code
      root.agentPromptsEnd(false)
    }
    // A start that fails (the command gone) gives neither end: Quickshell 0.3.1 only turns `running` off.
    onRunningChanged: {
      if (!agentPromptProc.running && agentPromptProc.code === null) root.agentPromptsEnd(true)
    }
  }

  // After a successful paste the token is usually still on the clipboard. `wl-copy --clear` is the
  // one-line way to take it off, offered rather than done: a clipboard is the user's.
  function clearClipboard() {
    Quickshell.execDetached(["wl-copy", "--clear"])
    logEvent(newTrace("clipboard"), "ui.accounts.clipboard", { cleared: true })
  }

  // Play goes through a Process, not execDetached: its JSON result (or error) drives `busy` and
  // `lastError`, which is what the popout shows while a release is being found.
  // `release` (an infohash) plays exactly that release from the episode's stored search: the
  // release picker's Enter.
  // A play asked for while this Service's own play is still starting is refused, out loud (unless that play is
  // waiting for TorBox and this one is another episode or a picked release: `Logic.playOverWait`):
  // `playRefusal` says so under the busy line, the refused request is logged under its own trace, and the caller
  // gets "" (the IPC, a sentence). Returning the running play's trace made Browse and the IPC treat it as started.
  function play(mediaId, episode, release) {
    if (!bin) return ""
    if (playProc.running && Logic.playOverWait(playEntry, lastTrace, mediaId, episode, Date.now() / 1000, release)) {
      // This Service's play is waiting for TorBox (that can last hours): another episode or show, or a release picked
      // for this one, goes ahead, detached like a play from the player's own list, and its claim supersedes the wait.
      // It is narrated off the poll.
      var overTrace = newTrace("play")
      logEvent(overTrace, "play.over_wait", { mediaId: Number(mediaId), episode: Number(episode),
                                              release: release || null,
                                              waiting: { trace: lastTrace, mediaId: playMediaId,
                                                         episode: playEpisode } })
      var overWords = [mediaId, episode, "--show"]
      if (release) overWords = overWords.concat(["--release", release])
      Quickshell.execDetached(Logic.cliLaunch([bin, "--trace", overTrace, "play"], overWords))
      expectPlayer("play")
      return overTrace
    }
    if (playProc.running) {
      playRefusal = Logic.playRefusalText({ mediaId: playMediaId, episode: playEpisode },
                                          { mediaId: Number(mediaId), episode: Number(episode) })
      // `autoNext`: the caller was auto-next, whose `autonext` line is just before this one.
      logEvent(newTrace("play"), "play.refused", { mediaId: Number(mediaId), episode: Number(episode),
                                                   release: release || null, holder: lastTrace,
                                                   autoNext: pendingAutoNext === true,
                                                   running: { mediaId: playMediaId, episode: playEpisode } })
      return ""
    }
    playRefusal = ""
    playHeld = ""
    var trace = newTrace("play")
    lastTrace = trace
    if (lastAdvance && Number(lastAdvance.episode) !== Number(episode)) lastAdvance = null
    clearError()
    playIsAutoNext = pendingAutoNext
    pendingAutoNext = false
    playMediaId = Number(mediaId)
    playEpisode = Number(episode)
    busy = release ? "Starting the release you chose for episode " + episode + "…"
                   : "Finding a release for episode " + episode + "…"
    logEvent(trace, "click", { action: "play", mediaId: mediaId, episode: episode, release: release || null })
    // `--no-notify`: this Process reads the answer and shows it, so a play refused before it starts (an entry that is
    // not out yet) is not also a notification.
    var words = [mediaId, episode, "--show", "--no-notify"]
    if (release) words = words.concat(["--release", release])
    Logic.cliOn(playProc, [bin, "--trace", trace, "play"], words)
    playProc.running = true
    expectPlayer("play")
    return trace
  }

  // One of a waiting play's choices: the command the entry gives it (`cli.wait_choice`), the same one the
  // player's key runs. Detached, like a play from the player's own episode list: "Watch the dub now" claims over the
  // waiting play (which may be this Service's own `playProc`; it then exits with `superseded`, not an error) and is
  // narrated through `status.play` like any play started elsewhere. Returns the click's trace, or "".
  function runChoice(button) {
    var launch = Logic.choiceLaunch(bin, newTrace("choice"), button ? button.args : null)
    if (!launch) {
      // A click that runs nothing says why (a sequence's args once did, silently).
      var why = Logic.choiceRefusal(bin, button ? button.args : null)
      var data = { id: String(button && button.id || ""), why: why }
      if (bin) Quickshell.execDetached(Logic.logEventLaunch(bin, newTrace("choice"), "ui", "ui.choice.refused", data,
                                                            "warn"))
      else console.warn("[omaanime] ui.choice.refused " + JSON.stringify(data))
      return ""
    }
    var trace = launch.command[2]
    logEvent(trace, "ui.choice", { id: String(button.id || ""), args: button.args,
                                   playTrace: playEntry ? String(playEntry.trace || "") : null })
    if (button.ack) {
      // "Tell me when it's ready": its answer is read (`laterProc`), so the viewer is told what `later` decided.
      if (laterProc.running) return ""
      laterProc.trace = trace
      laterProc.environment = launch.environment
      laterProc.command = launch.command
      laterProc.running = true
      return trace
    }
    Quickshell.execDetached(launch)
    if (String(button.id) === "dub" || String(button.id) === "wait") expectPlayer("choice")
    settle.restart()
    return trace
  }

  // The release picker's list. A search under 30 minutes old comes back from the CLI's
  // own store without touching an index; `refresh` (the picker's `r`) searches again.
  // The overlay, straight onto the release picker for one episode (popout button, IPC, mpv's p).
  function summonPicker(mediaId, episode) {
    if (!shell || typeof shell.summon !== "function") return false
    logEvent(newTrace("picker-summon"), "ui.picker.summon", { mediaId: mediaId, episode: episode })
    return shell.summon(pluginId, JSON.stringify({ picker: { mediaId: Number(mediaId), episode: Number(episode) } })) !== false
  }

  // A request made while a `releases` run was in flight used to be dropped: the sheet for the new
  // episode then showed the old one's answer filtered away (no rows, no message) or its error. Now the newest
  // request waits in `releasesQueued`, the run in flight is stopped (SIGTERM, Quickshell 0.3.1 docs), and the
  // queued one starts once that run has fully ended. What a stopped run still prints is not applied.
  property var releasesQueued: null
  property bool releasesExpectedStop: false
  property bool releasesExited: true
  property bool releasesStreamDone: true

  function loadReleases(mediaId, episode, refresh, query) {
    var req = { mediaId: Number(mediaId), episode: Number(episode), refresh: refresh === true,
                query: query ? String(query) : "" }
    var decision = Logic.releasesDecision({ bin: !!bin, running: releasesProc.running, exited: releasesExited,
                                            streamDone: releasesStreamDone, expectedStop: releasesExpectedStop,
                                            current: releasesProc.request, req: req })
    // The sheet speaks for the episode asked for, never for what an earlier request answered.
    if (!Logic.pickerDataFor(pickerData, req.mediaId, req.episode)) pickerData = null
    pickerError = ""
    if (decision === "refused") {
      pickerLoading = false
      pickerError = "the plugin's command was not found"
      return
    }
    pickerLoading = true
    if (decision === "start") {
      startReleases(req)
      return
    }
    if (decision === "join") {
      logEvent(releasesProc.trace || newTrace("releases"), "ui.picker.joined",
               { mediaId: req.mediaId, episode: req.episode, query: req.query || null })
      // Defence only: a join needs a run not being stopped, and a request waits only while one is being stopped.
      releasesQueued = null
      return
    }
    // The waiting request has its own trace from here on, and its run keeps it: the
    // `queued` line, the run's `load`, the CLI's own lines and its `exit` are one trace.
    req.trace = newTrace("releases")
    logEvent(req.trace, "ui.picker.queued",
             { mediaId: req.mediaId, episode: req.episode, refresh: req.refresh, query: req.query || null,
               replaces: releasesQueued,
               stopping: { trace: releasesProc.trace, mediaId: releasesProc.forMedia,
                           episode: releasesProc.forEpisode },
               running: releasesProc.running, exited: releasesExited, streamDone: releasesStreamDone })
    releasesQueued = req
    if (releasesProc.running && !releasesExpectedStop) {
      releasesExpectedStop = true
      releasesProc.running = false
    }
  }

  function startReleases(req) {
    releasesQueued = null
    releasesExpectedStop = false
    releasesExited = false
    releasesStreamDone = false
    pickerLoading = true
    var trace = req.trace || newTrace("releases")
    releasesProc.trace = trace
    releasesProc.request = req
    releasesProc.forMedia = req.mediaId
    releasesProc.forEpisode = req.episode
    logEvent(trace, "ui.picker.load", { mediaId: req.mediaId, episode: req.episode, refresh: req.refresh,
                                        query: req.query || null, queued: !!req.trace })
    var words = [req.mediaId, req.episode]
    if (req.query) words = words.concat(["--query", req.query])
    else if (req.refresh) words.push("--refresh")
    Logic.cliOn(releasesProc, [bin, "--trace", trace, "releases"], words)
    releasesProc.running = true
  }

  // A command that could not be started (the plugin's files gone, or the launcher's x bit lost,
  // while the shell runs). Quickshell 0.3.1 then sends neither a stream end nor `exited`, so without this the next
  // request would wait for them, and the sheet say "Searching…", until the shell restarts.
  function releasesStartFailed() {
    logEvent(releasesProc.trace || newTrace("releases"), "ui.picker.start_failed",
             { bin: bin, mediaId: releasesProc.forMedia, episode: releasesProc.forEpisode,
               queued: releasesQueued ? releasesQueued.trace || null : null })
    releasesExited = true
    releasesStreamDone = true
    if (!releasesQueued) {
      pickerLoading = false
      pickerError = "the plugin's command could not be started"
    }
    releasesRunEnded()
  }

  // Both ends of a run seen: the waiting request, if any, starts a turn later (never from inside the Process's own
  // signal), and only if nothing has started since.
  function releasesRunEnded() {
    if (!releasesExited || !releasesStreamDone) return
    releasesEndTimer.stop()
    if (!releasesQueued) return
    Qt.callLater(function () {
      if (root.releasesQueued && !releasesProc.running && root.releasesExited && root.releasesStreamDone)
        root.startReleases(root.releasesQueued)
    })
  }

  // One formatting of the structured error for both surfaces.
  //
  // The headline used to be "<stage> — <error>", which put a stage name
  // in the first two words of every failure the user ever saw, and the CLI's own advice was then
  // dropped entirely whenever candidates were present — which is every resolve failure. The CLI now
  // leads with a plain sentence naming the cause, so the headline *is* that sentence, and the stage
  // moves down into the detail line with everything else that is for diagnosis rather than action.
  function errorHeadline() {
    return lastError
  }

  // What to do about it, alone: the one surface-independent half. The bar panel has room for this
  // and not for the rest.
  function errorAction() {
    if (lastError === "") return ""
    return Logic.errorAction(lastErrorSuggest)
  }

  function errorDetail() {
    if (lastError === "") return ""
    // Ordered by what the user can act on, because the overlay caps this at three lines and elides
    // the rest: the advice, then the two keys, and the release names last. Earlier the names
    // came first and pushed both keys — and the CLI's advice, which was never shown at all — off the
    // end. A retry is worth naming first now that it means something: it waits the full budget.
    var parts = []
    var action = Logic.errorAction(lastErrorSuggest)
    if (action !== "") parts.push(action)
    parts.push("Enter retries and waits longer")
    if (lastErrorTrace !== "") parts.push("t copies the trace id")
    if (lastErrorPicker) parts.push("p chooses a release")
    var n = lastErrorCandidates ? lastErrorCandidates.length : 0
    if (n > 0) {
      // Release names run to 80 characters; three of them would elide the actionable half of the
      // line away, so each is cut to something still recognisable (group, title, resolution).
      var shortNames = []
      for (var i = 0; i < Math.min(3, n); i++) {
        var t = String(lastErrorCandidates[i])
        shortNames.push(t.length > 46 ? t.substring(0, 45) + "…" : t)
      }
      parts.push((n === 1 ? "1 release found, none usable: " : n + " releases found, none usable: ")
                 + shortNames.join(", "))
    } else if (lastErrorStage !== "") {
      parts.push(lastErrorStage)
    }
    return parts.join(" · ")
  }

  // `t` in the overlay: the trace id is the one thing that makes a failure diagnosable afterwards
  // (`omarchy-anime trace <id>`), and retyping a random hex id from a screenshot is hopeless.
  function copyTrace() {
    var trace = lastErrorTrace || lastTrace
    if (!trace) return ""
    Quickshell.execDetached(["wl-copy", "--", trace])
    logEvent(trace, "trace.copy", { trace: trace })
    return trace
  }

  function clearError() {
    lastError = ""
    lastErrorStage = ""
    lastErrorSuggest = ""
    lastErrorCandidates = []
    lastErrorTrace = ""
    lastErrorHeadline = ""
    lastErrorAction = ""
    lastErrorHint = ""
    lastErrorPicker = false
  }

  function playNext(entry) {
    if (!entry || !entry.media) return
    // Nothing for an entry that is not out, whatever `next` says (`Logic.nextToPlay`).
    var ep = Logic.nextToPlay(entry)
    if (!ep) return
    play(entry.media.id, ep)
  }

  function formatTime(seconds) {
    var t = Math.max(0, Math.floor(Number(seconds) || 0))
    var h = Math.floor(t / 3600)
    var m = Math.floor((t % 3600) / 60)
    var s = t % 60
    function pad(n) { return n < 10 ? "0" + n : String(n) }
    return h > 0 ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s)
  }

  // ------------------------------------------------------------ mpv socket
  // Protocol: newline-delimited JSON, verified live on mpv 0.41 (2026-09-14).
  // observe_property ids 10..15; get_property replies carry request_id 1 (time-pos) and 2 (duration).
  // The socket is read straight off the Loader rather than through the `socketConnected` binding:
  // inside onConnectedChanged the binding may not have been re-evaluated yet, and dropping the
  // observe_property batch sent from there leaves the service connected but blind (seen live:
  // socketConnected true, idle stuck true, mediaTitle empty, while mpv had a file loaded).
  function mpvSend(command, requestId) {
    var sock = mpvLoader.item
    if (!sock || sock.connected !== true) return
    sock.write(JSON.stringify({ command: command, request_id: requestId || 0 }) + "\n")
    sock.flush()
  }

  function onMpvConnected() {
    running = true
    reconnect.interval = 1000
    socketEvent({ type: "connected" })
    socketWarning = ""
    var props = ["pause", "idle-active", "eof-reached", "paused-for-cache", "media-title", "duration", "sub-delay"]
    for (var i = 0; i < props.length; i++) mpvSend(["observe_property", 10 + i, props[i]], 0)
    mpvSend(["get_property", "time-pos"], 1)
    refresh()
  }

  function onMpvDisconnected() {
    running = false
    socketEvent({ type: "disconnected" })
    paused = false
    idle = true
    eof = false
    cacheStalled = false
    timePos = 0
    duration = 0
    mediaTitle = ""
    reconnect.interval = 1000
    reconnect.restart()
  }

  // A *failed* connect attempt is not a disconnect: Qt emits `errorOccurred` and no `disconnected`,
  // so `connected` never changed and onMpvDisconnected() is not called. It does not start the down
  // clock (with no player there is nothing down); the backoff timer decides the next attempt.
  function onMpvError(err) {
    if (root.running) root.onMpvDisconnected()
    else reconnect.restart()
  }

  function onMpvMessage(line) {
    var msg
    try {
      msg = JSON.parse(line)
    } catch (e) {
      return
    }
    if (!msg || typeof msg !== "object") return
    if (msg.event === "property-change") {
      var v = msg.data
      switch (msg.name) {
      case "pause": paused = v === true; break
      case "idle-active":
        idle = v === true
        if (idle) { timePos = 0; duration = 0; eof = false }
        break
      case "eof-reached":
        var was = eof
        eof = v === true
        if (eof && !was) tick(true)
        break
      case "paused-for-cache": cacheStalled = v === true; break
      case "media-title": mediaTitle = v ? String(v) : ""; break
      case "duration": duration = Number(v) || 0; break
      case "sub-delay": subDelay = Number(v) || 0; break
      }
      return
    }
    if (msg.event === "file-loaded") {
      eof = false
      mpvSend(["get_property", "time-pos"], 1)
      settle.restart()
      return
    }
    if (msg.request_id === 1 && msg.error === "success") {
      timePos = Number(msg.data) || 0
    }
  }

  // The Socket is built from a Component and **thrown away after every failed attempt**.
  // Quickshell 0.3.1's Socket::setConnected(true) creates a QLocalSocket only while its internal
  // pointer is null (src/io/socket.cpp:75-84), and that pointer is cleared in exactly one place —
  // Qt's `disconnected` signal — which a failed *connect* never emits. So one refused connection
  // (mpv restarted, stale socket file) turns every later `connected = true` into a silent no-op:
  // no attempt, no error, no log line, and the socket stays dead until the shell is restarted.
  // Writing `connected = false` first does not help; `disconnectFromServer()` on an unconnected
  // QLocalSocket returns without emitting anything. A fresh object is the only way back, verified
  // in a throwaway `qs -p` shell against a socket server that was killed and restarted.
  Component {
    id: mpvSocketComponent
    Socket {
      path: root.socketPath
      connected: false
      onConnectedChanged: connected ? root.onMpvConnected() : root.onMpvDisconnected()
      onError: function (err) { root.onMpvError(err) }
      parser: SplitParser {
        splitMarker: "\n"
        onRead: function (line) { root.onMpvMessage(line) }
      }
    }
  }

  Loader {
    id: mpvLoader
    active: false
    sourceComponent: mpvSocketComponent
  }

  // When the current down episode started (only while a player runs), what it has cost, whether it has
  // warned, and whether a recent poll saw no player at all. `Logic.socketStep` owns every change to it; the warn
  // line and the popout notice are built from it, and a successful connect resets it.
  property var socketState: Logic.socketInit()
  property int socketPid: 0             // the mpv the current backoff has been failing against
  property string socketTrace: ""

  // One event through `Logic.socketStep`. Its log line is written under the episode's trace before that trace is
  // dropped; a new episode gets a new trace (`down -> restored|ended` in one `omarchy-anime trace <id>`).
  function socketEvent(ev) {
    ev.now = Date.now()
    var step = Logic.socketStep(socketState, ev)
    socketState = step.state
    if (step.opened) socketTrace = newTrace("socket")
    if (step.log) logService(step.log.stage, step.log.data, step.log.level)
    if (step.closed) socketTrace = ""
    return step
  }

  // This plugin starts or shows a player: try the socket in about a second, whatever the last poll said.
  function expectPlayer(why) {
    socketEvent({ type: "expect", why: String(why) })
    if (socketConnected) return
    reconnect.interval = 1000
    reconnect.restart()
  }

  // One attempt: drop the old object (it may be wedged), build a new one, ask it to connect.
  function socketConnect() {
    if (socketConnected || !socketPath) return
    mpvLoader.active = false
    mpvLoader.active = true
    if (mpvLoader.item) mpvLoader.item.connected = true
  }

  Timer {
    id: reconnect
    interval: 1000
    repeat: false
    onTriggered: {
      // No attempt (and no reschedule) while a recent poll saw no player; that poll or a play wakes it.
      var step = root.socketEvent({ type: "due", connected: root.socketConnected, path: root.socketPath })
      if (!step.attempt) return
      root.socketConnect()
      interval = Logic.backoffNext(interval, 15000)
      restart()
    }
  }

  // Position poll while something is loaded; the observed properties cover everything else.
  Timer {
    interval: root.pollIntervalSec * 1000
    repeat: true
    running: root.running && !root.idle
    onTriggered: root.mpvSend(["get_property", "time-pos"], 1)
  }

  // Step the clock locally between polls so the progress bar moves smoothly.
  Timer {
    interval: 1000
    repeat: true
    running: root.playing && !root.cacheStalled
    onTriggered: {
      if (root.duration > 0 && root.timePos >= root.duration) return
      root.timePos += 1
    }
  }

  // ----------------------------------------------------------- CLI status
  // `settingsEntry` is the input to every settings write (the entry is replaced
  // wholesale, so the copy has to be current). A `status` run that started *before* a write
  // carries the pre-write entry, and adopting it silently resurrects a key the write removed.
  // Stamp when each run starts and refuse a payload older than the last successful write.
  property double settingsWrittenAt: 0
  property double statusStartedAt: 0

  function refresh() {
    if (!bin || statusProc.running) return
    statusStartedAt = Date.now()
    statusProc.command = [bin, "status"]
    statusProc.running = true
  }

  Process {
    id: statusProc
    stdout: StdioCollector {
      onStreamFinished: {
        var s
        try {
          s = JSON.parse(this.text)
        } catch (e) {
          return
        }
        if (!s || typeof s !== "object") return
        root.windowVisible = s.visible === true
        root.parked = s.parked === true
        // The decision moved into `Logic.shouldHealPlayer` so it is testable under Node,
        // and so it stands down while the play that owns this player is still loading — `player
        // start` kills the hung pid, which used to destroy the very mpv a slow load was opening.
        if (Logic.shouldHealPlayer(s, root.healing, Date.now() / 1000).heal) {
          // mpv's core can stop answering IPC; `player start` kills and replaces it.
          // `clearError()` first, or a still-set `lastErrorHeadline` from
          // a previous failure wins in Panel.qml's rendering and this sentence never reaches the
          // user at all.
          root.healing = true
          root.clearError()
          root.lastError = "Player stopped answering; restarting it"
          healCooldown.restart()
          root.run("healPlayer", ["player", "start"])
        } else if (s.responsive === true && root.lastError.indexOf("stopped answering") !== -1) {
          root.clearError()
        }
        // Warn while there is still time to re-authenticate. The claim is decoded from the
        // token the CLI already holds, so this costs no request; the notice clears itself once a
        // new token is in place.
        var tok = s.anilist && typeof s.anilist === "object" ? s.anilist : null
        root.tokenWarning = Logic.tokenWarningText(tok, "shell")   // `notice` is drawn by popout and overlay
        // The 90 % step: say it in the UI while there is still budget to say it with.
        var rate = tok && tok.rate && typeof tok.rate === "object" ? tok.rate : null
        root.rateWarning = rate && rate.warn === true
          ? "AniList is nearly rate-limited: " + rate.remaining + " of " + rate.limit
            + " requests left, resets in " + rate.resetIn + "s"
          : ""
        root.subLang = s["current-tracks/sub/lang"] ? String(s["current-tracks/sub/lang"]) : ""
        root.nowPlaying = s.nowPlaying && typeof s.nowPlaying === "object" ? s.nowPlaying : null
        root.resume = s.resume && typeof s.resume === "object" ? s.resume : null
        if (s.settings && typeof s.settings === "object") root.settings = s.settings
        if (s.settingsSource && typeof s.settingsSource === "object") root.settingsSource = s.settingsSource
        // Only when this run is newer than the last write we made.
        if (s.settingsEntry && typeof s.settingsEntry === "object"
            && root.statusStartedAt >= root.settingsWrittenAt)
          root.settingsEntry = s.settingsEntry
        if (s.settingsReadOnly && typeof s.settingsReadOnly === "object") root.settingsReadOnly = s.settingsReadOnly
        root.languageNames = Logic.languageNamesOf(s, root.languageNames)
        if (s.configPath) root.configPath = String(s.configPath)
        if (typeof s.socketPath === "string") root.reportedSocketPath = s.socketPath
        // The socket is not allowed to be the only path to a progress write. While it is
        // down and the CLI says mpv is up with a file loaded, this poll drives the same `tick` the
        // socket would have driven, from the position it has already fetched.
        var fb = Logic.fallbackTick(s, root.socketConnected, root.nowPlaying)
        if (fb.tick) root.tickAt(fb.pos, fb.dur, fb.eof, "status-poll")
        // mpv up + socket down for over a minute is a fault, and it gets said out loud —
        // once in the log at warn level, and continuously in the popout until it clears. The
        // clock counts only while a player runs, so a player that just appeared is not "down 14 min".
        // A failed `status` ({ok: false}) has no `running`: unknown, not "no player".
        var sock = root.socketEvent({ type: "poll", running: typeof s.running === "boolean" ? s.running : null,
                                      connected: root.socketConnected })
        if (sock.warn)
          root.logService("socket.down", { downSeconds: sock.warn.seconds, attempts: sock.state.attempts,
                                           socketPath: root.socketPath, mpvPid: s.pid || null,
                                           fallbackTick: fb.tick, fallbackReason: fb.reason }, "warn")
        root.socketWarning = sock.notice.warn ? sock.notice.text : ""
        // mpv loads `--script=` once, at process start. The CLI does the comparison; this
        // says it in the popout, and once in the JSONL the first time each stale episode is seen —
        // a warn line every ten seconds would be noise, and silence is what made a stale player expensive.
        var stale = Logic.staleNotice(s)
        if (stale.warn && !root.staleWarned) {
          root.staleWarned = true
          root.logService("player.stale", { pid: s.pid || null, file: stale.file,
                                            reason: stale.reason }, "warn")
        } else if (!stale.warn) {
          root.staleWarned = false
        }
        root.staleWarning = stale.warn ? stale.text : ""
        // `busy` and `lastError` used to describe only the plays this service had spawned
        // itself. `status.play` is the shared entry every play writes — from the popout, from the
        // in-player panel, or from a terminal — so both now describe whichever play is real.
        // Kept raw, not just rendered into `busy`, so Panel.qml can show every
        // stage the play has passed (`Logic.progressRows`), not only the current one's sentence.
        root.playEntry = s.play && typeof s.play === "object" ? s.play : null
        // A play started elsewhere since a refusal (the player's own list): the refusal is about another moment. A play
        // that was already running beside it (`refusalBeside`) is not one.
        if (root.playRefusal
            && Logic.refusalOutlived(root.playEntry, playProc.running, root.lastTrace, root.refusalBeside, false))
          root.playRefusal = ""
        var pn = Logic.playNotice(s.play, playProc.running, root.playSeenTrace, Date.now() / 1000)
        if (!playProc.running) {
          root.playHeld = pn.held
          root.playLater = pn.later
        }
        if (pn.adopt) {
          root.playSeenTrace = pn.trace
          root.clearError()
          root.lastError = pn.error
          root.lastErrorStage = pn.stage
          root.lastErrorSuggest = pn.suggest
          root.lastErrorTrace = pn.trace
          root.lastErrorHeadline = pn.headline
          root.lastErrorAction = pn.action
          root.lastErrorHint = pn.hint
          // Not `stage` — that is one of Log.emit's own parameter names, and this payload
          // is splatted into it. `failedStage` is what the play-status JSON calls it anyway.
          root.logService("play.failed", { trace: pn.trace, failedStage: pn.stage,
                                           episode: s.play ? s.play.episode : null,
                                           startedHere: false }, "warn")
          // A play this Service did not start — the player's own episode list runs
          // `omarchy-anime play` itself — refuses the same way, so it hands over the same way:
          // the picker, and, since the viewer is in the player, the notice and a borrowed `p`.
          root.lastErrorPicker = Logic.wantsPicker({ ok: false, stage: pn.stage })
          if (root.lastErrorPicker && s.play && Number(s.play.mediaId) > 0 && Number(s.play.episode) > 0) {
            root.playMediaId = Number(s.play.mediaId)
            root.playEpisode = Number(s.play.episode)
            root.pickerWanted(root.playMediaId, root.playEpisode)
            if (root.running)
              Quickshell.execDetached(Logic.cliLaunch([root.bin, "--trace", pn.trace, "picker-notice"],
                                                      [root.playMediaId, root.playEpisode]))
          }
        }
        // While our own Process runs it owns the line: its error carries the candidate list.
        if (!playProc.running) root.busy = pn.busy
        // The socket may have been asleep in backoff while mpv came up through the CLI. A *new*
        // mpv (different pid) has a brand new socket, so its backoff starts over at one second;
        // the same mpv we have already failed against keeps the backoff it has earned.
        // Note the `running` guard: restarting a 15 s timer from a 10 s poll would starve it and
        // the socket would never be retried at all.
        if (s.running === true && !root.socketConnected) {
          var pid = Number(s.pid) || 0
          if (pid !== root.socketPid) {
            root.socketPid = pid
            reconnect.interval = 1000
            reconnect.restart()
          } else if (!reconnect.running) {
            reconnect.restart()
          }
        }
      }
    }
  }

  Timer {
    id: statusPoll
    interval: 10000
    repeat: true
    running: root.bin !== ""
    triggeredOnStart: true
    onTriggered: {
      root.refresh()
      // A stood-down reconnect is asked again every poll. While the poll keeps seeing no player it stays
      // down; if the poll stops answering, the "no player" goes stale and the attempts resume.
      if (!root.socketConnected && !reconnect.running) reconnect.restart()
    }
  }

  // One quick follow-up read after a command, so a click feels instant.
  Timer { id: settle; interval: 350; onTriggered: root.refresh() }

  // A play refused before it started (an entry that is not out) is said for this long on Browse's status line and in
  // the popout, then the line goes. A refusal said while this Service's own play starts is that play's to clear.
  Timer {
    id: refusalClear
    interval: 15000
    onTriggered: if (Logic.refusalOutlived(null, playProc.running, root.lastTrace, root.refusalBeside, true))
                   root.playRefusal = ""
  }

  // A play started somewhere else reaches this service only through the 10 s poll, so its
  // stage and its second counter would arrive in ten-second steps. While one is in flight — and
  // only then — the poll runs at two seconds.
  //
  // `&& !playProc.running` used to restrict this to a play adopted
  // from elsewhere, on the theory that a play THIS service started narrates itself. It does not —
  // `busy` is a static sentence set once at click time and only cleared when `playProc` exits — and
  // now that `root.playEntry` (what `Logic.progressRows` reads) is the same field either way, the
  // argument this timer exists for applies just as much to a self-started play: without the 2 s
  // cadence, its rows would only advance every ten seconds, for the exact slow stretch the rows
  // exist to narrate. Gating on `root.busy` alone still stops the timer on its own for both cases:
  // `busy` is cleared unconditionally at the top of `playProc`'s own `onStreamFinished` (a self-
  // started play), and by `playNotice` once an adopted one finishes, fails, or goes quiet
  // (no new timer to leak either way).
  Timer {
    interval: 2000
    repeat: true
    running: root.busy !== "" && root.bin !== ""
    onTriggered: root.refresh()
  }

  property bool healing: false
  Timer { id: healCooldown; interval: 30000; onTriggered: root.healing = false }

  // Hyprland tells us the moment the special workspace opens or closes (socket2 `activespecial`,
  // observed: "special:omaanime,eDP-1" on show, ",eDP-1" on hide). The status poll is the fallback.
  Connections {
    target: Hyprland
    function onRawEvent(event) {
      if (!event || event.name !== "activespecial") return
      var data = String(event.data || "")
      var name = data.split(",")[0]
      if (name === "special:omaanime") root.windowVisible = true
      else if (name === "" && root.windowVisible) root.windowVisible = false
    }
  }

  // ---------------------------------------------------------------- play
  Process {
    id: releasesProc
    // The run's own trace, as `deviceProc.trace`: what the UI says about this run is logged under it, so
    // `omarchy-anime trace <it>` shows a stopped run's stop and exit after the CLI's own lines.
    property string trace: ""
    property var request: null       // what the run is fetching, for a request that may join it
    property int forMedia: 0
    property int forEpisode: 0
    stdout: StdioCollector {
      onStreamFinished: {
        root.releasesStreamDone = true
        if (root.releasesExpectedStop) {
          // A stopped run's last words belong to the request it was stopped for, not to the sheet.
          root.logEvent(releasesProc.trace || root.newTrace("releases"), "ui.picker.stopped",
                        { mediaId: releasesProc.forMedia, episode: releasesProc.forEpisode,
                          queued: root.releasesQueued })
        } else {
          var a = Logic.releasesAnswer(this.text)
          root.pickerLoading = false
          if (a.data) root.pickerData = a.data
          else root.pickerError = a.error
        }
        root.releasesRunEnded()
      }
    }
    // In Quickshell 0.3.1 (`src/io/process.cpp`) a run that ends sends its stream end, then `exited`, then this, with
    // `running` already false; a start that fails sends this alone. So `running` false and still no exit a turn later
    // is a start that failed.
    onRunningChanged: {
      if (releasesProc.running || root.releasesExited) return
      Qt.callLater(function () {
        if (!releasesProc.running && !root.releasesExited) root.releasesStartFailed()
      })
    }
    onExited: function (code, status) {
      root.releasesExited = true
      // `status` tells how it ended: 0 (NormalExit) is the CLI exiting with `code`; 1 (CrashExit) is a signal, which
      // is the stop above when `expectedStop` is true and a kill from outside (OOM, `kill`) when it is not. Qt
      // documents `code` as valid only for NormalExit; on a stop Quickshell 0.3.1 with Qt 6.11.2 gave code 15 (the
      // signal) and status 1, measured with a `qs` probe. 15 is observed, not a contract.
      root.logEvent(releasesProc.trace || root.newTrace("releases"), "ui.picker.exit",
                    { code: code, status: status, expectedStop: root.releasesExpectedStop,
                      streamDone: root.releasesStreamDone,
                      queued: root.releasesQueued ? root.releasesQueued.trace || null : null })
      if (!root.releasesStreamDone) releasesEndTimer.restart()
      root.releasesRunEnded()
    }
  }

  // If stdout's end never arrives after the exit, neither the sheet nor a waiting request waits forever for it.
  Timer {
    id: releasesEndTimer
    interval: 3000
    onTriggered: {
      if (root.releasesStreamDone) return
      root.logEvent(releasesProc.trace || root.newTrace("releases"), "ui.picker.stream_end_missing",
                    { mediaId: releasesProc.forMedia, episode: releasesProc.forEpisode, queued: root.releasesQueued })
      root.releasesStreamDone = true
      if (!root.releasesExpectedStop && !root.releasesQueued) {
        root.pickerLoading = false
        root.pickerError = "the release list could not be read"
      }
      root.releasesRunEnded()
    }
  }

  Process {
    id: laterProc
    property string trace: ""
    stdout: StdioCollector {
      onStreamFinished: {
        var answer = Logic.laterAnswer(this.text)
        root.logEvent(laterProc.trace, "ui.choice.later", { later: answer.later })
        root.laterAnswer = { later: answer.later, message: answer.message, trace: laterProc.trace }
        root.refresh()
      }
    }
  }

  Process {
    id: playProc
    stdout: StdioCollector {
      onStreamFinished: {
        root.busy = ""
        root.playRefusal = ""
        root.playHeld = ""
        root.playLater = ""
        // Cleared up front so a play this Service spawned itself, which never
        // goes through `Logic.playNotice`, cannot leave a *stale* sentence on screen from whatever
        // failed before it — every branch below either leaves this cleared or fully overwrites it.
        root.clearError()
        var r
        try {
          r = JSON.parse(this.text)
        } catch (e) {
          root.lastError = "play returned no JSON"
          root.lastErrorStage = "cli"
          root.lastErrorSuggest = "run: omarchy-anime trace " + root.lastTrace
          root.lastErrorTrace = root.lastTrace
          root.refresh()
          return
        }
        if (r && r.ok === false) {
          // Kept in pieces: the stage names what failed, the candidates say how far it got, and the
          // suggest is the CLI's own advice built from what the providers answered (cli.py
          // describe_probe_errors). Flattening them into one line lost which stage failed and what each
          // provider answered.
          root.lastError = String(r.error || r.stage || "play failed")
          root.lastErrorStage = String(r.stage || "")
          root.lastErrorSuggest = String(r.suggest || "")
          root.lastErrorCandidates = Logic.isList(r.candidates) ? r.candidates : []
          root.lastErrorTrace = String(r.trace || root.lastTrace)
          // This is the CLI's own direct JSON response, not the
          // play-status.json entry `Logic.playNotice` reads — but `cli.main`'s top-level `except
          // StageError` spreads `exc.data` (which `cli.failure_error` puts `say` in) into this same
          // JSON unfiltered, so it is there too. This is the *common* failure path: a user pressing
          // play themselves, not one adopted off the poll — it must render the sentence exactly as
          // richly as that one does, not fall back to the record just because it arrived here first.
          var said = Logic.errorSentences(r.say, root.lastError, root.lastErrorSuggest)
          root.lastErrorHeadline = said.headline
          root.lastErrorAction = said.action
          root.lastErrorHint = said.hint
          // The same failure is in the shared entry; this one is richer, so mark it seen.
          root.playSeenTrace = root.lastErrorTrace
          // A refusal, or every release failing, hands over to the picker.
          root.lastErrorPicker = Logic.wantsPicker(r)
          if (root.lastErrorPicker && root.playMediaId > 0) {
            root.pickerWanted(root.playMediaId, root.playEpisode)
            // The viewer is in the player, not the overlay; tell them there and lend them `p`.
            if (root.playIsAutoNext)
              Quickshell.execDetached(Logic.cliLaunch([root.bin, "--trace", root.lastErrorTrace, "picker-notice"],
                                                      [root.playMediaId, root.playEpisode]))
          }
        } else if (r && r.held === true) {
          // The wait ended without the episode: the choices are held, not failed. The entry comes with the answer, so
          // Browse shows the buttons now rather than at the next poll.
          if (r.play && typeof r.play === "object") root.playEntry = r.play
          root.playHeld = Logic.playNotice(r.play, false, "", Date.now() / 1000).held
          root.logEvent(root.lastTrace, "play.held", { episode: r.episode || null, trace: String(r.trace || "") })
        } else if (r && r.later === true) {
          // "Tell me when it's ready" ended this Service's own waiting play: the popout says it was heard.
          root.playLater = Logic.playNotice(r.play, false, "", Date.now() / 1000).later
        } else if (r && r.refused === "not_out") {
          // AniList lists the entry as not yet released: refused before anything started (no claim, no player, no
          // search). Said as a refusal, not an error; Browse stays open on it.
          root.playRefusal = String(r.message || "")
          root.refusalBeside = root.playEntry ? String(root.playEntry.trace || "") : ""
          refusalClear.restart()
          root.logEvent(root.lastTrace, "play.not_out", { mediaId: root.playMediaId, episode: r.episode || null })
        } else if (r && r.claimed === false) {
          // The episode is already being started by another process, which is publishing its own
          // progress. Nothing is wrong: the next poll picks its line up through `status.play`.
          // (`clearError()` already ran unconditionally at the top of this handler.)
          root.busy = String(r.message || "")
          root.logEvent(root.lastTrace, "play.already", { holder: String(r.holder || ""),
                                                          episode: r.episode || null })
        } else {
          root.clearError()
        }
        root.refresh()
      }
    }
  }

  // ---------------------------------------------------------------- tick
  // Every 10 s while a tracked episode is loaded, plus once immediately when mpv reports eof.
  // Python stores the position and applies the watched rule exactly once.
  function tick(atEof) {
    if (idle) return false
    return tickAt(timePos, duration, atEof || eof, "socket")
  }

  // One tick, whoever produced the numbers. `source` reaches the CLI's `tick/decide` line, so the
  // JSONL says which transport was alive when a progress write happened. The
  // `tickProc.running` guard is what keeps the socket and the status poll from overlapping; if one
  // ever slips through, Python's watched rule is idempotent and the duplicate costs a file write.
  function tickAt(pos, dur, atEof, source) {
    if (!bin || tickProc.running || !nowPlaying || !(Number(nowPlaying.mediaId) > 0)) return false
    var trace = String(nowPlaying.trace || lastTrace || "service")
    var words = [nowPlaying.mediaId, nowPlaying.episode, Number(pos).toFixed(3), Number(dur).toFixed(3),
                 "--source", source || "socket"]
    if (atEof) words.push("--eof")
    Logic.cliOn(tickProc, [bin, "--trace", trace, "tick"], words)
    tickProc.running = true
    return true
  }

  Timer {
    interval: 10000
    repeat: true
    running: root.running && !root.idle && root.nowPlaying !== null
    onTriggered: root.tick(false)
  }

  Process {
    id: tickProc
    stdout: StdioCollector {
      onStreamFinished: {
        var r
        try {
          r = JSON.parse(this.text)
        } catch (e) {
          return
        }
        if (!r || typeof r !== "object") return
        root.resume = { pos: r.position, watched: r.watched || r.already }
        // The write the tick just tried failed and was queued; try it again straight away rather
        // than leaving it for the ten-minute timer.
        if (r.progress && r.progress.queued === true) root.flushProgress("tick")
        if (r.watched === true) {
          root.refresh()
          root.refreshWatching()
        }
        // The gate used to live here as `r.watched === true && root.eof`, and those are
        // never true together except when one tick straddles the threshold and the end of the
        // file: `watched` fires once, at 85%, where `eof` is still false. The decision is in
        // ServiceLogic so the node suite can hold it to the tick sequences the log actually shows.
        var an = Logic.autoNextDecision(r, { autoNext: root.autoNext, eof: root.eof,
                                             lastAdvance: root.lastAdvance })
        if (an.play) {
          root.lastAdvance = { mediaId: an.mediaId, episode: an.episode }
          root.logEvent(root.newTrace("autonext"), "autonext",
                        { mediaId: an.mediaId, episode: an.episode, from: r.episode })
          // Set only around this call: `play()` returns early while another play runs, and a flag
          // left set would make the viewer's next manual play count as auto-next.
          root.pendingAutoNext = true
          root.play(an.mediaId, an.episode)
          root.pendingAutoNext = false
        }
      }
    }
  }

  // --------------------------------------------------- pending progress queue
  // The design: "AniList write failure: keep the pending progress in pending-progress.json and retry
  // on the next launch and every 10 minutes." The queue was written from the start and nothing
  // ever flushed it, so a write that failed while the network was down stayed
  // queued forever. The timer below is both halves: `triggeredOnStart` is the launch.
  function flushProgress(reason) {
    if (!bin || flushProc.running) return
    flushProc.command = [bin, "--trace", newTrace("flush-" + reason), "flush-progress"]
    flushProc.running = true
  }

  Process {
    id: flushProc
    stdout: StdioCollector {
      onStreamFinished: {
        var r
        try {
          r = JSON.parse(this.text)
        } catch (e) {
          return
        }
        if (!r || !Logic.isList(r.flushed) || r.flushed.length === 0) return
        // A flushed write changes the list on AniList, so the cached one is stale (the CLI already
        // unlinked its own cache; this re-reads it) and the popout should show the new episode.
        root.refreshWatching()
        root.refresh()
      }
    }
  }

  Timer {
    interval: 600000
    repeat: true
    running: root.bin !== ""
    triggeredOnStart: true
    onTriggered: root.flushProgress("timer")
  }

  // ------------------------------------------------------- Continue Watching
  // Covers are downloaded by the CLI and read from disk by the QML; this process never does https
  // (Qt's TLS init once crashed it). One detached warm per service lifetime, right after the first watching payload, so
  // the overlay's Continue Watching view is already warm the first time it is opened. The overlay
  // warms again on open (cheap: a warm with nothing to do costs ~12 ms of Python), which is what
  // picks up a show added to the list later.
  property bool coversWarmed: false
  function warmCovers() {
    if (!bin || coversWarmed) return
    coversWarmed = true
    Quickshell.execDetached([bin, "covers", "warm", "--view", "watching", "--budget", "20"])
  }

  function refreshWatching() {
    if (!bin) return
    if (watchingProc.running) {
      watchingStale = true
      return
    }
    watchingProc.command = [bin, "watching"]
    watchingProc.running = true
  }

  // The list views' state, logged once per change with the CLI's own trace id so
  // `omarchy-anime trace <cliTrace>` shows why. Not on every five-minute read: only transitions.
  property string watchingStateLogged: ""
  function noteWatchingState(failure, count, saved) {
    var state = failure ? failure.state : saved ? "saved" : (count > 0 ? "ok" : "empty")
    if (state === watchingStateLogged) return
    logEvent(newTrace("watching"), "ui.list.state",
             { view: "watching", state: state, from: watchingStateLogged || "loading", count: count,
               cliStage: failure ? failure.stage : "", reason: failure ? failure.reason : "",
               cliTrace: failure ? failure.trace : "", error: failure ? failure.error : "",
               dropsRows: Logic.listFailureDropsRows(failure),
               savedAt: saved ? saved.savedAt : 0, savedReason: saved ? saved.reason : "" })
    watchingStateLogged = state
  }

  Process {
    id: watchingProc
    stdout: StdioCollector {
      onStreamFinished: {
        // Superseded: the re-run `onExited` starts answers instead, so an answer read
        // before a sign-in or sign-out never lands on top of it.
        if (root.watchingStale) return
        var list = null
        try {
          list = JSON.parse(this.text)
        } catch (e) {
          list = null
        }
        var failure = Logic.listFailure(list)
        if (failure) {
          // Signed out or a refused token: the rows belong to an account this side can no longer
          // read. A network failure keeps the last good list.
          if (Logic.listFailureDropsRows(failure)) {
            root.watching = []
            root.watchingSaved = null
          }
          root.watchingFailure = failure
          root.noteWatchingState(failure, root.watching.length, null)
          return
        }
        // A fresh list, or the saved one AniList's outage answered with: its rows show either way.
        var rows = Logic.listAnswerRows(list)
        root.watching = rows
        root.watchingLoaded = true
        root.watchingFailure = null
        root.watchingSaved = Logic.listAnswerStale(list)
        root.noteWatchingState(null, rows.length, root.watchingSaved)
        root.warmCovers()
      }
    }
    onExited: function () {
      if (!root.watchingStale) return
      root.watchingStale = false
      root.refreshWatching()
    }
  }

  Timer {
    interval: 300000
    repeat: true
    running: root.bin !== ""
    triggeredOnStart: true
    onTriggered: root.refreshWatching()
  }

  // `manifest` may already be set when this object is created, in which case onBinChanged never
  // fires; Component.onCompleted covers that path. Both are idempotent.
  property bool booted: false
  function boot() {
    if (booted || !bin) return
    booted = true
    console.log("[omaanime] service boot, sourceDir=" + sourceDir + " socket=" + socketPath)
    refresh()
    refreshWatching()
    reconnect.interval = 1000
    reconnect.restart()
  }
  onBinChanged: boot()
  Component.onCompleted: boot()

  // --------------------------------------------------------------- IPC
  // `omarchy-shell omaanime <fn>`; keybinds in docs/using.md use these.
  IpcHandler {
    target: "omaanime"
    function toggleWindow(): string { root.toggleWindow(); return "ok" }
    function show(): string { root.showWindow(); return "ok" }
    function hide(): string { root.hideWindow(); return "ok" }
    function playPause(): string {
      if (!root.running || root.idle) return "nothing loaded"
      root.playPause()
      return "ok"
    }
    function stop(): string { root.stop(); return "ok" }
    function play(mediaId: string, episode: string): string {
      var m = parseInt(mediaId, 10), e = parseInt(episode, 10)
      if (!(m > 0) || !(e > 0)) return "usage: play <anilistMediaId> <episode>"
      // The trace id when the play started; a refusal starts with "refused: ", so a script can tell them apart.
      return root.play(m, e) || ("refused: " + (root.playRefusal || "the play did not start"))
    }
    // `omarchy-shell omaanime browse [watching|top|airing|search]` opens the Browse overlay, so a
    // keybind can land straight on a view. The shell facade only lets us summon our own id.
    function browse(view: string): string {
      var name = String(view || "").trim()
      var allowed = ["watching", "top", "airing", "search", "lists", "settings", "advanced", "accounts"]
      if (name !== "" && allowed.indexOf(name) === -1)
        return "usage: browse [" + allowed.join("|") + "]"
      if (!root.shell || typeof root.shell.summon !== "function") return "no shell api"
      root.logEvent(root.newTrace("browse-ipc"), "browse.ipc", { view: name })
      var payload = name === "" ? "{}" : JSON.stringify({ view: name, from: "ipc" })
      return root.shell.summon(root.pluginId, payload) === false ? "could not open the overlay" : "ok"
    }
    // `omarchy-shell omaanime picker <mediaId> <episode>` opens the overlay on the release
    // picker. mpv's borrowed `p` runs it; a keybind can too.
    function picker(mediaId: string, episode: string): string {
      var m = parseInt(mediaId, 10), e = parseInt(episode, 10)
      if (!(m > 0) || !(e > 0)) return "usage: picker <anilistMediaId> <episode>"
      return root.summonPicker(m, e) ? "ok" : "could not open the overlay"
    }
    // `omarchy-shell omaanime setSetting autoNext true` — the same write the settings sheet makes,
    // reachable from a keybind and from a terminal. The value is JSON, so a real boolean and a real
    // integer reach shell.json rather than the strings `omarchy bar set` writes without --json.
    function setSetting(key: string, valueJson: string): string {
      var name = String(key || "").trim()
      if (name === "") return "usage: setSetting <key> <json value>  |  setSetting <key> reset"
      if (String(valueJson || "").trim() === "reset")
        return root.resetSetting(name) ? "ok" : (root.settingsError || "refused")
      var value
      try {
        value = JSON.parse(String(valueJson))
      } catch (e) {
        return "value must be JSON: true, 90, \"ara\""
      }
      return root.updateSetting(name, value) ? "ok" : (root.settingsError || "refused")
    }

    // `omarchy-shell omaanime accounts` — the same object the sheet draws, from a terminal. It cannot
    // print a token: the CLI does not, and this only forwards.
    function accounts(): string {
      root.loadAccounts(false)
      return JSON.stringify(root.accounts)
    }

    function settings(): string {
      return JSON.stringify({ effective: root.settings, source: root.settingsSource,
                              entry: root.settingsEntry, readOnly: root.settingsReadOnly,
                              configPath: root.configPath })
    }

    function status(): string {
      return JSON.stringify({
        running: root.running, playing: root.playing, paused: root.paused, idle: root.idle,
        windowVisible: root.windowVisible, timePos: root.timePos, duration: root.duration,
        mediaTitle: root.mediaTitle, subLang: root.subLang, busy: root.busy, lastError: root.lastError,
        playSeenTrace: root.playSeenTrace,
        lastErrorStage: root.lastErrorStage, lastErrorSuggest: root.lastErrorSuggest,
        lastErrorCandidates: root.lastErrorCandidates, lastErrorTrace: root.lastErrorTrace,
        tokenWarning: root.tokenWarning, rateWarning: root.rateWarning, notice: root.notice,
        bin: root.bin, socketPath: root.socketPath, socketConnected: root.socketConnected, lastTrace: root.lastTrace,
        // Where the subtitles on screen came from, so `omarchy-shell omaanime status` answers
        // "why am I reading English?" from the terminal without opening the popout.
        nowPlaying: root.nowPlaying ? { mediaId: root.nowPlaying.mediaId, episode: root.nowPlaying.episode,
                                        title: root.nowPlaying.title, next: root.nowPlaying.next,
                                        subSource: root.nowPlaying.subSource || null,
                                        subBadge: root.nowPlaying.subBadge || null,
                                        subDonor: root.nowPlaying.subDonor || null,
                                        subOffset: root.nowPlaying.subOffset === undefined ? null : root.nowPlaying.subOffset,
                                        subTimingVerified: root.nowPlaying.subTimingVerified === undefined ? null : root.nowPlaying.subTimingVerified } : null
      })
    }
  }
}
