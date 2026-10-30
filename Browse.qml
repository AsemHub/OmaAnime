import QtQuick
import QtQuick.Controls
import QtQuick.Window
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "BrowseModel.js" as Model
import "ServiceLogic.js" as Logic

// Full-screen Browse overlay. Standalone panel contract of
// Omarchy's plugin host: the host creates this Item on summon, injects
// `shell`/`manifest`/`omarchyPath`/`service`, then calls open(payloadJson) and close().
//
// Because this plugin declares `overlay` next to `bar-widget`, shell.qml's
// isBarWidgetPanelPlugin() is false for it, so `omarchy-shell shell summon|toggle
// io.github.asemhub.omaanime` reaches this file instead of the bar popout. The popout stays on
// left-click of the bar icon (docs/using.md documents the change).
//
// Division of labour, same as the rest of the plugin: decisions live in the Python CLI, the
// pure list/grid math lives in BrowseModel.js (node-tested), and this file is layout, keys and
// process plumbing. Nothing here blocks: every CLI call is a Process.
Item {
  id: root

  // ------------------------------------------------------- host-injected contract
  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  property var shell: null
  property var manifest: null
  // Our own Service.qml instance (shell.qml hands it to panel/overlay kinds of the same plugin).
  property var service: null
  // After a plugin update without `omarchy restart shell` the Service and the ServiceLogic.js this
  // file imports are the previous version's, so every call to either goes through these two: a function the running
  // copy lacks stands in (BrowseModel.js `serviceBridge`) instead of throwing. Properties are still read off
  // `service`, which keeps their bindings live; an absent one reads as undefined. Built when `service` is set, not as a
  // binding: its member checks would make it depend on every Service property it looks at (measured in qml6).
  property var bridge: root.bridgeFor(null)
  readonly property var svc: bridge.svc
  readonly property var logic: bridge.logic
  onServiceChanged: root.bridge = root.bridgeFor(root.service)

  // A BrowseModel.js older than this file has no `serviceBridge`; then the calls go straight through.
  function bridgeFor(service) {
    if (typeof Model.serviceBridge === "function") return Model.serviceBridge(service, Logic)
    return { svc: service, logic: Logic, missing: [], line: "", notice: "" }
  }

  property bool opened: false

  readonly property string pluginId: manifest && manifest.id ? String(manifest.id) : "io.github.asemhub.omaanime"
  readonly property string bin: service && service.bin ? String(service.bin) : ""

  // ------------------------------------------------------------------ view state
  readonly property var viewIds: ["watching", "top", "airing", "search", "lists"]
  readonly property var viewNames: ["Continue Watching", "Top 100", "Airing", "Search", "Lists"]
  property string view: "watching"

  // One cursor per view so Tab keeps its place.
  property int selWatching: 0
  property int selTop: 0
  property int selAiring: 0
  property int selSearch: 0
  property int selLists: 0

  // ------------------------------------------------------------------- Lists tab
  // The other five AniList lists. A fifth tab rather than a status
  // cycle on Continue Watching: Enter means something different per status and Continue Watching's
  // Enter is the plugin's hot path, so overloading it behind an invisible mode flag is the exact
  // class of mode error the rest of the overlay avoids. The tab strip makes the mode visible.
  property string listStatus: "PLANNING"
  property var listsByStatus: ({})       // status -> payload, so switching back is free
  property bool listsLoading: false
  // Why the last `list` read did not return a list (`Logic.listFailure`), or null.
  property var listsFailure: null
  // status -> the CLI's `stale` marker when that status's rows are the saved list AniList's outage answered with
  // (`Logic.listAnswerStale`); a fresh read of the status removes it.
  property var listsSaved: ({})
  // Statuses whose rows predate an AniList account change; their next read skips the
  // CLI's five-minute cache. And a read asked for while one runs, re-run when it ends.
  property var listsStale: ({})
  property bool listsQueued: false

  // Set while a play started from this overlay is still resolving; it is what turns a `busy`
  // clear into "close the overlay, the player has it now".
  property bool playPending: false
  // The choices of a play waiting for TorBox's download (or holding them after the wait), from the shared
  // entry the Service polls: `Model.playChoice`'s view, or null. Re-read on every poll and once a second, so a
  // running entry nobody writes any more stops offering them.
  // A Service older than this file runs no choice (`runChoice` missing): no buttons and no keys then, and the status
  // line says to restart the shell instead (the bridge's notice ranks above the choices).
  property var playChoice: root.service && root.bridge.missing.indexOf("runChoice") < 0
                           ? Model.playChoice(root.service.playEntry, root.choiceClock) : null
  property real choiceClock: Date.now() / 1000
  // Whether the choices' keys apply where the card is: everywhere but another show's detail view, whose own keys win
  // (`w` sets that show as watching) and whose help line stays on the status line.
  readonly property bool choiceKeysHere: !root.detailOpen
                                          || (!!root.playChoice && root.detailMediaId === root.playChoice.mediaId)
  // The acknowledgement of a choice just made ("We'll tell you when episode 3 is ready."), shown until the overlay
  // closes a moment later.
  property string choiceAck: ""
  // The trace of the "Tell me when it's ready" whose answer this overlay waits for.
  property string choiceAckTrace: ""

  // Payloads of the three forked views. Each is fetched once on first entry; `r` refetches
  // with --refresh (the CLI caches top/airing for a day and watching for five minutes).
  property var topList: []
  property bool topLoading: false
  property bool topLoaded: false
  property var airingList: []
  property bool airingLoading: false
  property bool airingLoaded: false
  // The stale marker of the saved Top 100 / Airing an AniList outage answered with (`Logic.listAnswerStale`), or
  // null. The tab's next fresh read clears it, and the line with it.
  property var topSaved: null
  property var airingSaved: null
  property var searchList: []
  property bool searchLoading: false

  // Seeded from the widget setting, `c` overrides it for the session.
  property bool showCompleted: false

  // Detail view. It layers over whichever list opened it; the list keeps its
  // cursor, so Escape lands exactly where the user left.
  property bool detailOpen: false
  property int detailMediaId: 0
  property var detailMedia: null
  property bool detailLoading: false
  property string detailError: ""
  property int cellIndex: 0
  property bool settingWatching: false

  // ---------------------------------------------------------------- settings window
  // Not a fifth tab: settings are not a browsing view. Its own window (`settingsWindow`), opened on `,` from
  // anywhere in the overlay, from the popout's Settings button, and from the payload {"view":"settings"} — which
  // reaches open() because this plugin declares `overlay`, so shell.qml's isBarWidgetPanelPlugin is false for it and
  // the payload is not dropped (shell.qml:1178). Only `openSettings` and `closeSettings` change `settingsOpen`.
  property bool settingsOpen: false
  // Opened with no overlay behind it (the popout, `omarchy-shell omaanime browse settings`): closing it closes this
  // plugin's surface, instead of bringing the overlay back.
  property bool settingsAlone: false
  // Where it was opened from, for the open and close lines: "browse" (`,`, the ⚙ button), "popout", "ipc", "payload".
  property string settingsFrom: ""
  readonly property bool settingsShown: root.opened && root.settingsOpen
  onSettingsShownChanged: root.showSettingsWindow(root.settingsShown)
  // The release picker sheet. Opened by a refusal (Service.pickerWanted) or by `p`, for one
  // episode; its rows come from `releases` through Service.pickerData.
  property bool pickerOpen: false
  // The detail view's show's pin, from `pin show`; null when there is none.
  property var detailPin: null
  property int pickerMediaId: 0
  property int pickerEpisode: 0
  property int pickerIndex: 0
  readonly property var pickerData: service && service.pickerData
      && Number(service.pickerData.anilist_id) === pickerMediaId
      && Number(service.pickerData.episode) === pickerEpisode ? service.pickerData : null
  readonly property var pickerItems: Model.pickerItems(pickerData, pickerData ? pickerData.pinned : null)
  onPickerItemsChanged: {
    var first = Model.pickerFirst(pickerItems)
    if (pickerIndex >= pickerItems.length || !pickerItems[pickerIndex] || pickerItems[pickerIndex].kind !== "row")
      pickerIndex = Math.max(0, first)
  }

  // The rows are the manifest's own `barWidget.schema`. `publicPluginManifest` deletes only the
  // three `__` keys (shell.qml:316-324), so `barWidget` survives injection — verified live.
  readonly property var settingsRows: {
    var bw = root.manifest && root.manifest.barWidget ? root.manifest.barWidget : null
    return bw && Model.isList(bw.schema) ? bw.schema : []
  }

  function settingValue(key) {
    var s = root.service && root.service.settings ? root.service.settings : ({})
    var row = null
    for (var i = 0; i < root.settingsRows.length; i++)
      if (String(root.settingsRows[i].key) === String(key)) row = root.settingsRows[i]
    return s[key] !== undefined ? s[key] : (row && row.defaultValue !== undefined ? row.defaultValue : "")
  }

  // A language setting's choices carry their names; every other row's options are shown as they are.
  function optionChoices(row) {
    var opts = row && row.options ? row.options : []
    if (!row || (row.key !== "subtitleLanguage" && row.key !== "subtitleFallbackLanguage")) return opts
    return Model.languageOptions(opts, root.service ? root.service.languageNames : null)
  }

  // A menu that comes out empty although its row lists options warns once per key each time the sheet opens, with
  // the row's key and its options' type (the empty language menus were once found by eye, not in the log).
  property var menuWarned: ({})
  onSettingsOpenChanged: if (!settingsOpen) menuWarned = ({})
  function noteMenu(row) {
    if (!root.settingsOpen || !root.bin) return
    var report = Model.emptyMenuReport(row, root.optionChoices(row), root.menuWarned)
    if (!report) return
    root.menuWarned[report.key] = true
    var trace = (root.service ? root.svc.newTrace("settings") : "") || ("ui-settings-" + Math.floor(Date.now() / 1000))
    Quickshell.execDetached(Model.logEventArgv(root.bin, trace, "settings.menuEmpty", report, "warn"))
  }

  function settingSource(key) {
    var src = root.service && root.service.settingsSource ? root.service.settingsSource : ({})
    return src[key] ? String(src[key]) : "default"
  }

  function sourceCaption(key) {
    var where = root.settingSource(key)
    if (where === "shell") return "saved here, in Omarchy's shell.json"
    if (where === "config") return "set in " + (root.service ? root.service.configPath : "config.json")
    return ""
  }

  function writeSetting(key, value) {
    if (!root.service) return false
    if (root.settingValue(key) === value) return true      // no-op: never rewrite the bar config
    return root.svc.updateSetting(key, value)
  }

  function resetSetting(key) {
    if (!root.service) return false
    return root.svc.resetSetting(key)
  }

  // The Advanced disclosure. Collapsed by default: the sheet's first screen stays the
  // thirteen keys a user turns while watching, and the disclosure is the honest signal that the
  // other ten are not. Before this they were a read-only dump that named a file and told the user
  // to go and edit it by hand — which answers "where is that set?" and then refuses to help.
  property bool advancedOpen: false

  readonly property var sheetRows: Model.sheetRows(root.settingsRows)
  readonly property var advancedRows: Model.advancedRows(root.settingsRows)

  // How many advanced keys are not at their default, so the collapsed header can say so.
  function advancedChangedCount() {
    var n = 0
    for (var i = 0; i < root.advancedRows.length; i++)
      if (root.settingSource(String(root.advancedRows[i].key)) !== "default") n += 1
    return n
  }

  // One write, not ten: every advanced key leaves the entry together, so there is no
  // interleaving with the status poll to get wrong.
  function resetAdvanced() {
    if (!root.service) return false
    var keys = []
    for (var i = 0; i < root.advancedRows.length; i++) keys.push(String(root.advancedRows[i].key))
    return root.svc.resetSettings(keys)
  }

  // Two keys' control unit is not their stored unit: 16777216 (MiB) and 17179869184 (GiB) are not
  // numbers anybody wants to type, so the sheet shows the unit and converts on the way in and out.
  // `null` for every other key, which is the "no conversion" case.
  function byteUnitOf(key) { return Model.byteUnit(key) }

  // -------------------------------------------------------------------------------- accounts
  //
  // An Accounts section at the top of the settings sheet, not a sixth tab and not a separate
  // overlay: the sheet is already the plugin's one configuration surface, and an account is
  // something you touch twice a year.
  readonly property var accountProviders: ["anilist", "torbox", "realdebrid", "subdl", "opensubtitles"]
  readonly property var accountLabels: ({ anilist: "AniList", torbox: "TorBox", realdebrid: "Real-Debrid",
                                          subdl: "Subdl", opensubtitles: "OpenSubtitles" })
  property string signingIn: ""          // the provider whose paste field is open
  // The provider whose Save was refused because another sign-in was still being checked. Its token
  // stays in the field; the row says why. Browse's own, so the other sign-in's result cannot wipe it.
  property string signInWaiting: ""
  // TorBox signs in with a device code by default; this switches its row to the paste field.
  property bool torboxPaste: false
  readonly property var torboxDevice: root.service && root.service.torboxDevice
                                      ? root.service.torboxDevice : ({ state: "idle" })
  property string confirmingOut: ""      // the provider whose "Really sign out?" is showing

  function account(provider) {
    var a = root.service && root.service.accounts ? root.service.accounts : ({})
    return a[String(provider)] || ({})
  }

  // Which indexer add-ons loaded, in order, and which were refused and why. It rides on
  // the same `accounts` read the sheet already makes on open, so no second process.
  readonly property var indexerSection: Model.indexerSection(root.service && root.service.accounts
                                                             ? root.service.accounts.indexers : null)
  // The two AI agent buttons under it, each with the Service's line after a click (`agentNotes`).
  readonly property var agentRows: Model.agentRows(root.service ? root.service.agentNotes : null)

  // One line per provider (`Model.accountLine`). It carries a state, a plan and at most the last four
  // characters of the token — never the token.
  function accountLine(provider) {
    return Model.accountLine(provider, root.account(provider))
  }

  function accountColour(provider) {
    var a = root.account(provider)
    if (!a.present) return root.dim
    if (a.error || a.expired) return Commons.Color.accent
    if (a.expiring) return Commons.Color.accent
    return root.foreground
  }

  function openSignIn(provider) {
    root.signingIn = String(provider)
    root.signInWaiting = ""
    root.confirmingOut = ""
    if (root.service && "accountsError" in root.service) root.service.accountsError = ""
    if (String(provider) === "torbox") {
      root.torboxPaste = false
      if (root.service) root.svc.signInTorBoxDevice()
    }
  }

  // Cancel, from the row's button: a device wait in flight is stopped, not left to run on.
  function closeSignIn() {
    if (root.signingIn === "torbox" && root.service) root.svc.cancelTorBoxDevice()
    root.signingIn = ""
    root.signInWaiting = ""
    root.torboxPaste = false
  }

  // Torznab servers under Accounts. The rows come from the same `accounts` read (its `torznab` block) and
  // the Service's last check per server; adding, checking and removing run in the Service's own processes.
  readonly property var torznabSection: Model.torznabSection(root.service && root.service.accounts
                                                             ? root.service.accounts.torznab : null,
                                                             root.service ? root.service.torznabChecks : null)
  readonly property string torznabBusy: root.service ? String(root.service.torznabBusy || "") : ""
  // "up to N s", from the CLI's own check budget (`checkBudget` in the `torznab` block).
  readonly property string torznabWait: Model.torznabBudgetText(root.service && root.service.accounts
                                                                ? root.service.accounts.torznab : null)
  // A Prowlarr or Jackett found on this computer, offered until the list holds its address (right after a save
  // too, so adding Prowlarr leaves Jackett offered); its button opens the add row with the address filled in and the
  // key field focused. Nothing is saved until Add and check.
  readonly property var torznabOfferState: Model.torznabOfferDecision(
      root.logic.torznabListUsable(root.service && root.service.accounts ? root.service.accounts.torznab : null),
      root.service && root.service.accounts ? root.service.accounts.torznab : null,
      root.service ? root.service.torznabLocal : null)
  readonly property var torznabOffers: root.torznabOfferState.offers
  // The decision, once per open and per change: what was found, which of it is already listed, what is offered
  // (products only, never an address). Nothing until `find-local` has answered.
  property string torznabOfferLogged: ""
  onTorznabOfferStateChanged: root.noteTorznabOffers()
  function noteTorznabOffers() {
    if (!root.settingsOpen || !root.service || !root.service.torznabLocal) return
    var st = root.torznabOfferState
    var data = { action: "offer", found: st.found, listed: st.listed,
                 offered: st.offers.map(function (o) { return o.product }) }
    var key = JSON.stringify(data)
    if (key === root.torznabOfferLogged) return
    root.torznabOfferLogged = key
    root.svc.logEvent(root.svc.newTrace("torznab"), "ui.accounts.torznab", data)
  }
  property bool torznabAdding: false       // the add row is open
  property string torznabConfirm: ""       // the server whose "Really remove?" is showing

  Timer { id: torznabConfirmTimer; interval: 5000; onTriggered: root.torznabConfirm = "" }

  function pasteTorBoxInstead() {
    if (root.service) root.svc.cancelTorBoxDevice()
    root.torboxPaste = true
  }

  // Tab walks the sheet's controls through Qt's own focus chain, which does not scroll the
  // Flickable with it — a setting below the fold would take focus invisibly and the next keystroke
  // would edit something the user cannot see. Follow the focus instead.
  function inSheet(item) {
    var it = item
    while (it) {
      if (it === settingsFlick) return true
      it = it.parent
    }
    return false
  }

  function revealInSheet(item) {
    if (!root.settingsOpen || !item || !root.inSheet(item)) return
    var p = settingsColumn.mapFromItem(item, 0, 0)
    if (!p) return
    var pad = Style.space(12)
    var maxY = Math.max(0, settingsFlick.contentHeight - settingsFlick.height)
    if (p.y - pad < settingsFlick.contentY)
      settingsFlick.contentY = Math.max(0, p.y - pad)
    else if (p.y + item.height + pad > settingsFlick.contentY + settingsFlick.height)
      settingsFlick.contentY = Math.min(maxY, p.y + item.height + pad - settingsFlick.height)
  }

  function scrollSheet(dy) {
    var maxY = Math.max(0, settingsFlick.contentHeight - settingsFlick.height)
    settingsFlick.contentY = Math.max(0, Math.min(maxY, settingsFlick.contentY + dy))
  }

  function openPicker(mediaId, episode, refresh) {
    if (!root.service || !(Number(mediaId) > 0) || !(Number(episode) > 0)) return
    if (root.settingsOpen) {
      root.settingsAlone = false        // the picker is on the overlay, so the overlay stays
      root.closeSettings("picker")
    }
    root.pickerMediaId = Number(mediaId)
    root.pickerEpisode = Number(episode)
    root.pickerIndex = 0
    root.pickerOpen = true
    pickerField.text = ""
    root.svc.loadReleases(mediaId, episode, refresh === true)
    root.svc.logEvent(root.svc.newTrace("picker"), "ui.picker.open",
                      { mediaId: Number(mediaId), episode: Number(episode), refresh: refresh === true,
                        from: root.detailOpen ? "detail" : root.view })
  }

  function closePicker() {
    if (!root.pickerOpen) return
    root.pickerOpen = false
    if (root.detailOpen) root.loadPin()
    Qt.callLater(function () { if (root.opened) keyCatcher.forceActiveFocus() })
  }

  // `/`: the viewer's own words, searched by the indexer add-ons that offer text search;
  // with none, `/` is not offered.
  function pickerQuery(words) {
    var w = String(words || "").trim()
    if (!root.service || w === "" || !Model.pickerOffersWords(root.pickerData)) return
    root.pickerIndex = 0
    root.svc.loadReleases(root.pickerMediaId, root.pickerEpisode, false, w)
    Qt.callLater(function () { if (root.opened) keyCatcher.forceActiveFocus() })
  }

  function pickerMove(delta) {
    root.pickerIndex = Model.pickerStep(root.pickerItems, root.pickerIndex, delta)
  }

  // Enter: exactly this release. The sheet stays up under the busy line; a success dismisses
  // the overlay like any play, a failure comes back here with the row marked (Service.pickerWanted).
  function pickerPlay() {
    var item = root.pickerItems[root.pickerIndex]
    if (!root.service || !item || item.kind !== "row") return
    var trace = root.svc.play(root.pickerMediaId, root.pickerEpisode, item.hash)
    if (!trace) return
    root.playPending = true
    root.svc.logEvent(trace, "browse.pick", { mediaId: root.pickerMediaId, episode: root.pickerEpisode,
                                              release: item.hash, rejected: item.dim, index: root.pickerIndex })
  }

  // `from` says where (`settingsFrom`). Asked again while it is open, the window is shown again rather than reset,
  // so a paste field or a sign-in in progress keeps its state.
  function openSettings(from) {
    var where = String(from || "browse")
    if (root.settingsOpen) {
      if (root.service) root.svc.logEvent(root.svc.newTrace("settings"), "ui.settings.open",
                                          { from: where, again: true, alone: root.settingsAlone })
      root.raiseSettings()
      return
    }
    root.settingsOpen = true
    root.settingsFrom = where
    root.torznabOfferLogged = ""
    if (!root.service) return
    root.svc.logEvent(root.svc.newTrace("settings"), "ui.settings.open", { from: where, alone: root.settingsAlone })
    // The providers come from the ten-minute cache; the indexer add-ons are read fresh,
    // so a file dropped in or fixed shows the next time the sheet opens. The accounts first, the extra after;
    // the find is decided when this read answers, a later turn.
    root.svc.loadAccounts(false)
    root.svc.wantLocalTorznab()     // look for a Prowlarr or Jackett on this computer
  }

  // `how`: "escape", "button" (✕), "window" (the compositor closed it), "picker", "summon" (Browse asked for), "host".
  // Alone, the plugin's surface goes with it; else the overlay comes back with the keyboard.
  function closeSettings(how) {
    if (!root.settingsOpen) return
    var alone = root.settingsAlone
    root.settingsOpen = false
    root.settingsAlone = false
    if (root.service)
      root.svc.logEvent(root.svc.newTrace("settings"), "ui.settings.close",
                        { how: String(how || ""), from: root.settingsFrom, then: alone ? "closed" : "browse" })
    if (alone) {
      root.dismiss()
      return
    }
    Qt.callLater(function () { if (root.opened) keyCatcher.forceActiveFocus() })
  }

  function showSettingsWindow(shown) {
    settingsWindow.visible = shown
    if (shown) Qt.callLater(function () { if (root.settingsShown) settingsKeys.forceActiveFocus() })
  }

  // Shown again, so a window left on another workspace or behind the browser comes to where the viewer is: a new
  // surface, mapped where a new window goes (the items, and every field's text, are kept).
  function raiseSettings() {
    if (!root.settingsShown) return
    settingsWindow.visible = false
    Qt.callLater(function () { root.showSettingsWindow(root.settingsShown) })
  }

  // Search runs one live AniList call per query. `searchSerial` rises with every request the
  // user's typing implies; a result whose serial is no longer the newest is dropped, so a slow
  // answer for "fri" can never overwrite the answer for "pellin".
  property int searchSerial: 0
  property int searchInFlight: -1
  property bool searchExpectedStop: false
  property bool searchQueued: false

  // Minute clock so "airs in 3d 4h" stays honest while the overlay is open.
  property var nowDate: new Date()
  readonly property real nowMs: nowDate.getTime()

  // Shown for two seconds after `t` copies the failing play's trace id to the clipboard.
  property string copiedTrace: ""
  Timer { id: copiedTraceTimer; interval: 2500; onTriggered: root.copiedTrace = "" }
  // The sign-out confirm falls back to "Sign out" on its own, so a stray click cannot arm a
  // destructive button and leave it armed.
  Timer { id: confirmTimer; interval: 5000; onTriggered: root.confirmingOut = "" }

  // Instrumentation. `openT0` starts when the host calls open(); the first time a view
  // actually has rows on screen, one `ui browse.open.ms` line is written under the open's trace
  // with {view, ms, rows}. That number — time to rows, not time to covers — is the one the cover
  // cache must not make worse, so it is measured before the cover work and again after it.
  property real openT0: 0
  property string openTrace: ""
  property var openReported: ({})

  function noteRowsShown() {
    if (!root.service || root.openT0 <= 0 || root.rows.length === 0) return
    if (root.openReported[root.view]) return
    var seen = root.openReported
    seen[root.view] = true
    root.openReported = seen
    root.svc.logEvent(root.openTrace || root.svc.newTrace("browse-open"), "browse.open.ms",
                      { view: root.view, ms: Math.round(Date.now() - root.openT0), rows: root.rows.length })
  }

  onRowsChanged: root.noteRowsShown()

  function open(payloadJson) {
    var payload = ({})
    try {
      payload = JSON.parse(payloadJson || "{}") || ({})
    } catch (e) {
      payload = ({})
    }
    // `{"view":"advanced"}` is `settings` with the disclosure already open — a direct route to the
    // ten keys behind it, the same way `settings` is a direct route to the sheet.
    var wanted = String(payload.view || "")
    var wantsSettings = wanted === "settings" || wanted === "advanced" || wanted === "accounts"
    if (wantsSettings) {
      // With no overlay up, Settings comes alone; over an open overlay it closes back to it.
      if (!root.settingsOpen) root.settingsAlone = !root.opened
      if (wanted === "advanced") root.advancedOpen = true
    } else if (root.settingsOpen) {
      // Browse asked for while Settings is up: the overlay would cover the window, so the window closes and the
      // overlay shows (a TorBox sign-in keeps waiting in the Service; reopening Settings shows it again).
      root.settingsAlone = false
      root.closeSettings("summon")
    }
    if (!root.settingsOpen) {
      root.signingIn = ""
      root.confirmingOut = ""
    }
    if (payload.view && root.viewIds.indexOf(String(payload.view)) !== -1) root.view = String(payload.view)
    root.playPending = false
    root.openT0 = Date.now()
    root.openReported = ({})
    root.coversDone = ({})
    if (root.service && root.service.settings)
      root.showCompleted = root.logic.asBool(root.service.settings.showCompletedInTop, false)
    root.opened = true
    root.ensureLoaded()
    // The window opens at the top, which is where Accounts is, so `accounts` needs no scrolling — only the state:
    // the providers from a 10-minute cache, the indexer add-ons read fresh (`openSettings`).
    if (wantsSettings) root.openSettings(String(payload.from || "payload"))
    // `{"picker":{"mediaId":…,"episode":…}}` lands on the release picker (the popout's
    // button, `omarchy-shell omaanime picker`, and mpv's borrowed `p` after a refused auto-next).
    if (payload.picker && Number(payload.picker.mediaId) > 0 && Number(payload.picker.episode) > 0)
      root.openPicker(payload.picker.mediaId, payload.picker.episode, false)
    // A Service older than this file says so once per open, here and in the JSONL, naming what it
    // lacks and the command that loads the new one; bindings that use the stand-ins never log.
    var stale = root.bridge.missing.length > 0
    var openLine = "[omaanime] browse open, service=" + (root.service ? (stale ? "stale" : "ok") : "null")
                   + " payload=" + JSON.stringify(payload)
    if (stale) console.warn(openLine + "; " + root.bridge.line)
    else console.log(openLine)
    if (root.service) {
      var trace = root.svc.newTrace("browse-open") || ("ui-browse-open-" + Math.floor(Date.now() / 1000))
      root.openTrace = trace
      root.svc.logEvent(trace, "browse.open", { view: root.view, mediaId: payload.mediaId || null })
      if (stale && root.bin)
        Quickshell.execDetached(Model.logEventArgv(root.bin, trace, "browse.serviceStale",
                                                   { view: root.view, missing: root.bridge.missing,
                                                     fix: "omarchy restart shell" }, "warn"))
      root.svc.refreshWatching()
      root.noteRowsShown()
    }
    // The layer surface is mapped only once `visible` turns true, so focus has to be
    // re-acquired a turn later or Escape lands nowhere (Menu.qml and wifiqr do the same).
    // A payload that asks for the Search view must land in the field, not on the list:
    // otherwise the first keystrokes are read as list commands and Escape closes the overlay.
    Qt.callLater(function () {
      if (!root.opened) return
      if (root.view === "search" && !root.detailOpen) {
        searchField.forceActiveFocus()
        searchField.selectAll()
      } else {
        keyCatcher.forceActiveFocus()
      }
    })
  }

  function close() {
    if (root.settingsOpen) {
      root.settingsAlone = false        // the host is closing everything already
      root.closeSettings("host")
    }
    root.opened = false
    root.playPending = false
  }

  // Dismissing ourselves goes through the host so openPanelIds stays in sync.
  function dismiss() {
    if (root.shell && typeof root.shell.hide === "function") root.shell.hide(root.pluginId)
    else root.close()
  }

  // ------------------------------------------------------------------- appearance
  // The menu palette is the one themed for full-screen summoned surfaces.
  readonly property color background: Commons.Color.menu.background
  readonly property color foreground: Commons.Color.menu.text
  readonly property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.55)
  readonly property color scrim: Commons.Color.menu.scrim
  // The cursor fill for rows and cells. `Style.selectedFillFor` rather than
  // `Commons.Color.menu.selectedBackground`: it is the same token every other Omarchy panel row uses,
  // and it has to carry the mouse highlight too now that hover moves the cursor instead of
  // painting its own (the `Ui/CursorSurface` contract).
  readonly property color selectedBackground: Style.selectedFillFor(foreground, Commons.Color.accent)
  readonly property var borderSpec: Border.surfaceSpec("menu", "border", Commons.Color.menu.border,
                                                       Math.max(1, Style.space(2)))
  readonly property string fontFamily: Style.font.menuFamily

  readonly property int rowHeight: Style.space(52)
  readonly property int coverHeight: rowHeight - Style.space(10)
  readonly property int coverWidth: Math.round(coverHeight * 2 / 3)

  // ------------------------------------------------------------------- row models
  // Each view maps its CLI payload through BrowseModel.js and then into one delegate shape:
  //   { mediaId, episode, rankText, cover, title, caption, right, rewatch }
  readonly property var watchingRows: {
    var out = []
    if (!service || !Model.isList(service.watching)) return out
    for (var i = 0; i < service.watching.length; i++) {
      var r = Model.rowText(service.watching[i], nowMs)
      if (!r.mediaId) continue
      out.push({ mediaId: r.mediaId, episode: r.episode, rankText: "", cover: r.cover,
                 title: r.title, caption: "", right: r.detail, rewatch: r.rewatch })
    }
    return out
  }

  readonly property var topRows: {
    var out = []
    var src = Model.topRows(topList, showCompleted)
    for (var i = 0; i < src.length; i++) {
      var r = src[i]
      out.push({ mediaId: r.mediaId, episode: null, rankText: r.rankText, cover: r.cover, title: r.title,
                 caption: r.statusWord + (r.listState ? " · " + r.listState : ""),
                 right: r.scoreText, rewatch: false })
    }
    return out
  }

  readonly property var airingRows: {
    var out = []
    if (!Model.isList(airingList)) return out
    for (var i = 0; i < airingList.length; i++) {
      var r = Model.airingRow(airingList[i], nowMs)
      if (!r.mediaId) continue
      out.push({ mediaId: r.mediaId, episode: null, rankText: "", cover: r.cover, title: r.title,
                 caption: r.caption, right: r.detail, rewatch: false })
    }
    return out
  }

  readonly property var searchRows: {
    var out = []
    var src = Model.searchRows(searchList)
    for (var i = 0; i < src.length; i++) {
      var r = src[i]
      out.push({ mediaId: r.mediaId, episode: null, rankText: "", cover: r.cover, title: r.title,
                 caption: r.detail, right: r.scoreText, rewatch: false })
    }
    return out
  }

  readonly property var listsRows: {
    var out = []
    var payload = root.listsByStatus[root.listStatus]
    var src = Model.listRows(Model.isList(payload) ? payload : [], root.listStatus)
    for (var i = 0; i < src.length; i++) {
      var r = src[i]
      out.push({ mediaId: r.mediaId, episode: r.episode, rankText: "", cover: r.cover, title: r.title,
                 caption: r.statusWord, right: r.detail, rewatch: false, action: r.action })
    }
    return out
  }

  // --------------------------------------------------------------------- covers
  // Nothing in this process ever fetches an image (quickshell crashed in Qt's TLS init
  // inside QQuickPixmapReader, on the first https request of the process — a cover). The CLI
  // downloads covers into ~/.cache/omaanime/covers and every payload carries `coverPath`;
  // this file turns a path into a file:// URL and refuses anything else.
  function coverUrl(p) {
    // The leading-slash test makes "no https ever reaches Qt" a property of the code rather than
    // of whatever the CLI happened to return.
    return p && String(p).charAt(0) === "/" ? Util.fileUrl(String(p)) : ""
  }

  // Warm state: which view the running warm is for, when it started, and which views have already
  // been warmed during this open (a re-read finishes by landing here again, so without the guard
  // the payload → warm → re-read → warm loop would run one redundant process per view).
  property string coversView: ""
  property real coversT0: 0
  property var coversDone: ({})

  function warmCovers(view) {
    if (!bin || coversProc.running) return
    var argv = [bin, "covers", "warm", "--budget", "8"]
    if (view === "search") {
      var ids = [], bad = []
      for (var i = 0; i < searchList.length; i++) {
        if (!searchList[i]) continue
        var mid = Model.mediaIdArg(searchList[i].id)
        if (mid) ids.push(mid)
        else bad.push(String(searchList[i].id).slice(0, 40))
      }
      // An id that is not an AniList id is never handed to the CLI; one line says how many and the first few.
      if (bad.length > 0) console.warn("[omaanime] covers.warm media_refused " + JSON.stringify({ view: view,
        refused: bad.length, kept: ids.length, first: bad.slice(0, 3) }))
      if (ids.length === 0) return
      // A search result is not on disk anywhere, and a 10-row search must not pay a second AniList
      // call: the CLI reads the cover URLs out of its own title cache, so `--media` is enough.
      argv = argv.concat(["--media"]).concat(ids)
    } else {
      if (coversDone[view]) return
      var done = coversDone
      done[view] = true
      coversDone = done
      argv = argv.concat(["--view", view])
    }
    coversView = view
    coversT0 = Date.now()
    coversProc.command = argv
    coversProc.running = true
  }

  // Search results are replaced wholesale rather than mutated, so the rows rebind.
  function mergeSearchCovers(map) {
    if (!map || !Model.isList(searchList) || searchList.length === 0) return
    var out = []
    for (var i = 0; i < searchList.length; i++) {
      var m = searchList[i]
      var cp = m && m.id !== undefined && m.id !== null ? map[String(m.id)] : null
      if (!cp) {
        out.push(m)
        continue
      }
      var copy = ({})
      for (var k in m) copy[k] = m[k]
      copy.coverPath = cp
      out.push(copy)
    }
    searchList = out
  }

  Process {
    id: coversProc
    stdout: StdioCollector {
      onStreamFinished: {
        var r
        try {
          r = JSON.parse(this.text)
        } catch (e) {
          return
        }
        if (!r || typeof r !== "object") return
        var view = root.coversView
        if (root.service)
          root.svc.logEvent(root.openTrace || root.svc.newTrace("browse-covers"), "browse.covers.ms",
                            { view: view, requested: r.requested || 0, hit: r.hit || 0,
                              downloaded: r.downloaded || 0, failed: r.failed || 0,
                              ms: Math.round(Date.now() - root.coversT0) })
        if (!r.downloaded) return
        // Something new landed on disk: re-read the same list command (a JSON-cache read plus one
        // process start) so the rows pick up their paths. The dumbest mechanism that works — no
        // file watcher, no marker file, no new IPC, and it reuses the code path `r` already uses.
        if (view === "search") root.mergeSearchCovers(r.media)
        else if (view === "top") root.loadTop(false)
        else if (view === "airing") root.loadAiring(false)
        else if (view === "lists") root.loadLists(false)
        else if (view === "watching" && root.service) root.svc.refreshWatching()
      }
    }
  }

  function rowsFor(name) {
    switch (name) {
    case "watching": return watchingRows
    case "top": return topRows
    case "airing": return airingRows
    case "search": return searchRows
    case "lists": return listsRows
    default: return []
    }
  }

  readonly property var rows: rowsFor(view)

  // The line over a list drawn from the saved copy AniList's outage answered with, or "": Continue
  // Watching's marker is the Service's, each Lists status's its own read's. `atMs` is the overlay's minute clock
  // (`nowMs`), so "14:05" turns into "yesterday at 14:05" at midnight without waiting for the next read.
  function savedLineText(atMs) {
    if (view === "lists")
      return logic.listStaleText({ stale: listsSaved[listStatus] || null, nowMs: atMs,
                                   what: "your " + Model.listStatusLabel(listStatus) + " list" })
    if (view === "top") return logic.listStaleText({ stale: root.topSaved || null, what: "Top 100", nowMs: atMs })
    if (view === "airing") return logic.listStaleText({ stale: root.airingSaved || null, what: "Airing", nowMs: atMs })
    if (view !== "watching" || !service) return ""
    return logic.listStaleText({ stale: service.watchingSaved || null, what: "your list", nowMs: atMs })
  }

  // A Top 100 or Airing answer (`view`): its rows, fresh or the saved copy an AniList outage answered with, and its
  // stale marker, or null for anything else (a failure, no JSON: the tab keeps what it shows). Entering or leaving
  // the saved copy is logged once, as the lists' is (`ui.list.state`).
  function tabAnswer(view, list) {
    var rows = root.logic.listAnswerRows(list)
    if (rows === null) return null
    var saved = root.logic.listAnswerStale(list)
    var was = view === "top" ? root.topSaved : root.airingSaved
    var loaded = view === "top" ? root.topLoaded : root.airingLoaded
    if (!!saved !== !!was && root.service)
      root.svc.logEvent(root.svc.newTrace("browse-" + view), "ui.list.state",
                        { view: view, state: saved ? "saved" : rows.length > 0 ? "ok" : "empty",
                          from: was ? "saved" : loaded ? "ok" : "loading", count: rows.length,
                          savedAt: saved ? saved.savedAt : 0, savedReason: saved ? saved.reason : "" })
    return { rows: rows, saved: saved }
  }

  function selIndex() {
    switch (view) {
    case "top": return selTop
    case "airing": return selAiring
    case "search": return selSearch
    case "lists": return selLists
    default: return selWatching
    }
  }

  function setSel(index) {
    var n = rows.length
    var i = n > 0 ? Math.max(0, Math.min(index, n - 1)) : 0
    switch (view) {
    case "top": selTop = i; break
    case "airing": selAiring = i; break
    case "search": selSearch = i; break
    case "lists": selLists = i; break
    default: selWatching = i; break
    }
  }

  function moveSel(delta) {
    if (rows.length === 0) return
    setSel(selIndex() + delta)
  }

  function currentRow() {
    var i = selIndex()
    return i >= 0 && i < rows.length ? rows[i] : null
  }

  // --------------------------------------------------------------------- actions
  function setView(name) {
    if (root.viewIds.indexOf(name) === -1 || name === root.view) return
    root.view = name
    if (root.service) root.svc.logEvent(root.svc.newTrace("browse-view"), "browse.view", { view: name })
    root.ensureLoaded()
    if (name === "search") Qt.callLater(function () { if (root.opened) searchField.forceActiveFocus() })
    else Qt.callLater(function () { if (root.opened) keyCatcher.forceActiveFocus() })
  }

  // Forked views fetch on first entry, so opening the overlay costs nothing extra, and again on each entry while they
  // show a saved copy.
  function ensureLoaded() {
    if (!bin) return
    // A tab showing its saved copy reads again on every entry, so AniList's first answer takes the line away.
    if (view === "top" && (!topLoaded || root.topSaved) && !topLoading) loadTop(false)
    else if (view === "airing" && (!airingLoaded || root.airingSaved) && !airingLoading) loadAiring(false)
    // The watching list is already in the service; its covers are the only thing that may be
    // missing, so the warm starts straight away instead of waiting for a payload.
    else if (view === "lists") loadLists(false)
    else if (view === "watching") warmCovers("watching")
  }

  // One request per status, on demand, 5-minute cache in the CLI. At AniList's degraded 30 req/min a
  // fully warmed set of five statuses costs a sixth of the per-minute budget.
  function loadLists(force) {
    if (!bin) return
    if (listsProc.running) {
      // Not dropped. `onExited` re-reads whichever status is showing by then.
      root.listsQueued = true
      return
    }
    var status = root.listStatus
    var stale = !!root.listsStale[status]
    if (!force && !stale && Model.isList(root.listsByStatus[status])) {
      root.warmListCovers()
      return
    }
    if (stale) {
      var left = ({})
      for (var k in root.listsStale) if (k !== status) left[k] = true
      root.listsStale = left
    }
    root.listsLoading = true
    listsProc.status = status
    listsProc.superseded = false
    listsProc.command = force || stale ? [bin, "list", "--status", status, "--refresh"]
                                       : [bin, "list", "--status", status]
    listsProc.running = true
  }

  function setListStatus(name) {
    if (Model.LIST_STATUSES.indexOf(name) === -1 || name === root.listStatus) return
    root.listStatus = name
    root.selLists = 0
    if (root.service) root.svc.logEvent(root.svc.newTrace("browse-lists"), "ui.browse.lists",
                                        { status: name })
    root.loadLists(false)
  }

  function cycleListStatus(delta) {
    var i = Model.LIST_STATUSES.indexOf(root.listStatus)
    if (i < 0) i = 0
    setListStatus(Model.LIST_STATUSES[(i + delta + Model.LIST_STATUSES.length) % Model.LIST_STATUSES.length])
  }

  // The rows carry `coverPath` only once `covers warm` has downloaded them; the shell never fetches
  // an image itself. Same second-pass shape the other views use.
  function warmListCovers() {
    if (!bin || coversProc.running) return
    var payload = root.listsByStatus[root.listStatus]
    if (!Model.isList(payload) || payload.length === 0) return
    var key = "lists:" + root.listStatus
    if (coversDone[key]) return
    var done = coversDone
    done[key] = true
    coversDone = done
    var ids = [], bad = []
    for (var i = 0; i < payload.length; i++) {
      var m = payload[i] && payload[i].media ? payload[i].media : null
      if (!m) continue
      var mid = Model.mediaIdArg(m.id)
      if (mid) ids.push(mid)
      else bad.push(String(m.id).slice(0, 40))
    }
    if (bad.length > 0) console.warn("[omaanime] covers.warm media_refused " + JSON.stringify({ view: "lists",
      refused: bad.length, kept: ids.length, first: bad.slice(0, 3) }))
    if (ids.length === 0) return
    coversView = "lists"
    coversT0 = Date.now()
    coversProc.command = [bin, "covers", "warm", "--budget", "8", "--media"].concat(ids)
    coversProc.running = true
  }

  function loadTop(force) {
    if (!bin || topProc.running) return
    topLoading = true
    topProc.command = force ? [bin, "top", "--refresh"] : [bin, "top"]
    topProc.running = true
  }

  function loadAiring(force) {
    if (!bin || airingProc.running) return
    airingLoading = true
    airingProc.command = force ? [bin, "airing", "--refresh"] : [bin, "airing"]
    airingProc.running = true
  }

  function toggleCompleted() {
    showCompleted = !showCompleted
    setSel(0)
    if (service) root.svc.logEvent(root.svc.newTrace("browse-completed"), "browse.showCompleted", { on: showCompleted })
  }

  // ---------------------------------------------------------------------- search
  function requestSearch() {
    searchSerial += 1
    var q = String(searchField.text).replace(/^\s+|\s+$/g, "")
    if (q === "") {
      searchList = []
      searchLoading = false
      searchQueued = false
      if (searchProc.running) { searchExpectedStop = true; searchProc.running = false }
      return
    }
    if (searchProc.running) {
      // The newest query wins; stop the one in flight and restart from onExited (the wifiqr
      // cancellation pattern — onExited and onStreamFinished have no ordering guarantee).
      searchQueued = true
      if (!searchExpectedStop) { searchExpectedStop = true; searchProc.running = false }
      return
    }
    startSearch(q)
  }

  function startSearch(q) {
    if (!bin || q === "") return
    searchExpectedStop = false
    searchInFlight = searchSerial
    searchLoading = true
    searchProc.command = [bin, "search", q, "--limit", "10"]
    searchProc.running = true
  }

  function focusSearch() {
    setView("search")
    Qt.callLater(function () {
      if (!root.opened) return
      searchField.forceActiveFocus()
      searchField.selectAll()
    })
  }

  function cycleView(delta) {
    var i = root.viewIds.indexOf(root.view)
    if (i < 0) i = 0
    setView(root.viewIds[(i + delta + root.viewIds.length) % root.viewIds.length])
  }

  // The overlay stays up while the CLI hunts for a release; `busy` drives the status line and
  // clearing it with no error is what closes us.
  function playRow(row) {
    if (!service || !row || !row.mediaId || !row.episode) return
    var trace = root.svc.play(row.mediaId, row.episode)
    if (!trace) return
    playPending = true
    // Logged under the trace `service.play` minted, so `omarchy-anime trace last` shows the
    // click and the resolve stages as one timeline rather than two.
    root.svc.logEvent(trace, "browse.play", { mediaId: row.mediaId, episode: row.episode,
                                              rewatch: row.rewatch === true, view: root.view })
  }

  function activate() {
    var row = currentRow()
    if (!row) return
    // A row with nothing to play (an entry not out yet, or whose first episode has not aired) opens its episodes.
    if (root.view === "watching") {
      if (row.episode) playRow(row)
      else if (row.mediaId) openDetail(row.mediaId)
    }
    else if (root.view === "lists") activateListRow(row)
    else openDetail(row.mediaId)
  }

  // Enter means something different per status, which is the whole reason Lists is its own tab:
  //   start — the show is not on Watching yet: `progress <id> 0` (status CURRENT, progress kept at
  //           0), then play episode 1. The only AniList write in this view.
  //   play  — resume at progress + 1.
  //   grid  — open the episode grid. Completed and Dropped land here: Enter on a 1100-episode
  //           completed show must not silently start episode 1.
  function activateListRow(row) {
    if (row.action === "play") {
      playRow(row)
      return
    }
    if (row.action === "start") {
      startRow(row)
      return
    }
    openDetail(row.mediaId)
  }

  function startRow(row) {
    if (!bin || !row || !row.mediaId || startProc.running) return
    root.startingMediaId = Number(row.mediaId)
    root.startingEpisode = Number(row.episode) || 1
    if (root.service) root.svc.logEvent(root.svc.newTrace("browse-start"), "ui.browse.start",
                                        { mediaId: root.startingMediaId, status: root.listStatus })
    startProc.command = [bin, "progress", String(row.mediaId), "0"]
    startProc.running = true
  }

  property int startingMediaId: 0
  property int startingEpisode: 1

  Process {
    id: startProc
    onExited: function (code) {
      var mediaId = root.startingMediaId
      var episode = root.startingEpisode
      root.startingMediaId = 0
      if (code !== 0 || !mediaId) return
      // The write moved the entry from Planning to Watching; both caches are already invalidated by
      // the CLI, so re-read this list and the service's watching list, then play.
      root.loadLists(true)
      if (root.service) root.svc.refreshWatching()
      root.playRow({ mediaId: mediaId, episode: episode, rewatch: false })
    }
  }

  // ---------------------------------------------------------------- detail view
  // The CLI's episode payload (pictures, titles, resume) once `episodes --fetch` returns;
  // until then the instant number-only cells from the media alone, so the grid never waits.
  property var detailPayload: null
  // The trace `loadDetail` minted for `episodesProc`, so its own exit/parse log lines join the
  // same timeline as the process's own `episodes` CLI trace instead of starting a new one.
  property string episodesTrace: ""
  // Which show each of the two processes was actually started for. `detailMediaId` is the show the
  // user is looking at NOW, and the two are not the same thing the moment someone opens a second
  // show while the first one's processes are still running — which is the normal case, since
  // `episodes --write --fetch` takes seconds. Without these, a payload was matched against whatever
  // show happened to be open when it landed, and the exit line logged that show's id too.
  property int detailProcMediaId: 0
  property int episodesProcMediaId: 0
  // Instrumentation, the same shape `openT0`/`openTrace` use for the overlay itself. The
  // goal is "the detail view opens fast enough to feel instant", and nothing
  // measured it — "feels fast" is not evidence. `detailT0` starts when the `media` call is issued,
  // `detailTrace` is the trace `openDetail` mints, so the wait, the CLI run behind it and the
  // episodes fetch beside it all read as one timeline under `omarchy-anime trace <id>`.
  property real detailT0: 0
  property string detailTrace: ""
  readonly property var detailCells: detailPayload && Model.isList(detailPayload.episodes)
                                     && detailPayload.episodes.length > 0
    ? detailPayload.episodes
    : (detailMedia ? Model.episodeCells(detailMedia) : [])
  readonly property var detailShow: detailPayload ? detailPayload : ({})
  readonly property var detailHeader: detailMedia ? Model.detailHeader(detailMedia) : null
  readonly property int gridColumns: Model.gridColumns(grid.width, Style.space(180))

  function openDetail(mediaId) {
    if (!mediaId) return
    detailMediaId = Number(mediaId)
    detailMedia = null
    detailPayload = null
    detailError = ""
    detailPin = null
    cellIndex = 0
    detailOpen = true
    if (service) {
      root.detailTrace = root.svc.newTrace("browse-detail")
      root.svc.logEvent(root.detailTrace, "browse.detail", { mediaId: detailMediaId, from: root.view })
    }
    loadDetail()
    Qt.callLater(function () { if (root.opened) keyCatcher.forceActiveFocus() })
  }

  function closeDetail() {
    detailOpen = false
    detailMedia = null
    detailPayload = null
    detailError = ""
    detailLoading = false
    Qt.callLater(function () {
      if (!root.opened) return
      if (root.view === "search") searchField.forceActiveFocus()
      else keyCatcher.forceActiveFocus()
    })
  }

  function loadPin() {
    if (!bin || !detailMediaId || pinProc.running) return
    pinProc.forMedia = detailMediaId
    pinProc.command = [bin, "pin", "show", String(detailMediaId)]
    pinProc.running = true
  }

  // `x` in the picker: the pin for the show the picker is open on.
  function forgetPin() {
    if (!bin || !root.pickerMediaId || forgetPinProc.running) return
    var trace = service ? root.svc.newTrace("pin") : "pin"
    if (service) root.svc.logEvent(trace, "ui.pin.forget", { mediaId: root.pickerMediaId })
    forgetPinProc.command = [bin, "--trace", trace, "pin", "forget", String(root.pickerMediaId)]
    forgetPinProc.running = true
  }

  function loadDetail() {
    loadPin()
    loadMedia()
    loadEpisodes()
  }

  // The two halves are separate functions rather than one body with an inner `if`, because each is
  // retriggered on its own when its process turns out to have been running for a show the user has
  // already left. Folding the episodes half back into `loadDetail` would gate that retrigger on
  // `detailProc.running` — and "the detail process is still running" is exactly the state a quick
  // second open leaves behind, so the retrigger would no-op precisely when it is needed.
  function loadMedia() {
    if (!bin || !detailMediaId || detailProc.running) return
    detailLoading = true
    root.detailT0 = Date.now()
    root.detailProcMediaId = detailMediaId
    detailProc.command = [bin, "media", String(detailMediaId)]
    detailProc.running = true
  }

  function loadEpisodes() {
    if (!bin || !detailMediaId || episodesProc.running) return
    var trace = service ? root.svc.newTrace("browse-episodes") : ""
    root.episodesTrace = trace
    root.episodesProcMediaId = detailMediaId
    var argv = [bin]
    if (trace) argv = argv.concat(["--trace", trace])
    episodesProc.command = argv.concat(["episodes", String(detailMediaId), "--write", "--fetch"])
    episodesProc.running = true
  }

  function setCell(index) {
    var n = detailCells.length
    cellIndex = n > 0 ? Math.max(0, Math.min(index, n - 1)) : 0
  }

  function moveCell(delta) {
    if (detailCells.length === 0) return
    setCell(cellIndex + delta)
  }

  // The cursor starts on NEXT, which on a long-running show is hundreds of cells down, and the
  // viewport opened at card 1 regardless -- so on HARBOR LIGHTS the cursor was simply off screen, the
  // grid looked like it had ignored where the user is up to, and the first arrow key appeared to
  // do nothing. Called wherever the model or the cursor has just been (re)bound.
  // `Qt.callLater` because the model is assigned in the same statement block: the delegates
  // `positionViewAtIndex` needs to measure do not exist yet this frame.
  function positionOnCursor() {
    Qt.callLater(function () {
      if (!root.detailOpen || root.detailCells.length === 0) return
      grid.positionViewAtIndex(Math.max(0, Math.min(root.cellIndex, root.detailCells.length - 1)),
                               GridView.Center)
    })
  }

  function playCell() {
    var cell = detailCells[cellIndex]
    if (!cell || cell.state === "unaired") return
    playRow({ mediaId: detailMediaId, episode: cell.episode, rewatch: cell.state === "watched" })
  }

  // "Set as watching": status CURRENT, existing progress kept (0 for a show that
  // is not on the list yet). The CLI verifies the write with a read-back and clears the cached
  // watching list, so the reload below and refreshWatching() show the real state.
  function setWatching() {
    if (!bin || !detailMediaId || watchProc.running) return
    var progress = detailHeader ? detailHeader.progress : 0
    settingWatching = true
    var trace = service ? root.svc.newTrace("browse-watching") : ""
    if (service) root.svc.logEvent(trace, "browse.setWatching", { mediaId: detailMediaId, progress: progress })
    var argv = [bin]
    if (trace) argv = argv.concat(["--trace", trace])
    watchProc.command = argv.concat(["progress", String(detailMediaId), String(progress)])
    watchProc.running = true
  }

  Process {
    id: detailProc
    stdout: StdioCollector {
      onStreamFinished: {
        root.detailLoading = false
        var media
        try {
          media = JSON.parse(this.text)
        } catch (e) {
          root.detailError = "could not read the anime details"
          return
        }
        if (!media || typeof media !== "object" || !media.id) {
          root.detailError = "could not read the anime details"
          return
        }
        // The same guard `episodesProc` has always had: this process may have been started for a
        // show the user has already left, and painting its header over the show now open is worse
        // than painting nothing. `onExited` restarts the load for the show that IS open.
        if (Number(media.id) !== root.detailMediaId) return
        root.detailError = ""
        root.detailMedia = media
        root.cellIndex = Model.initialCellIndex(Model.episodeCells(media))
        root.positionOnCursor()
        if (root.service && root.detailT0 > 0)
          root.svc.logEvent(root.detailTrace || root.svc.newTrace("browse-detail"),
                            "browse.detail.ms",
                            { mediaId: root.detailMediaId, ms: Math.round(Date.now() - root.detailT0) })
      }
    }
    onExited: function (code) {
      root.detailLoading = false
      var wanted = root.detailProcMediaId
      if (root.detailOpen && root.detailMediaId !== wanted) {
        // This ran for a show the user has left. The open show never got a `media` call at all
        // (`loadMedia` refused while this one was running), so it would sit with an empty header
        // forever; start it now.
        root.loadMedia()
        return
      }
      if (code !== 0 && !root.detailMedia) root.detailError = "omarchy-anime media failed (exit " + code + ")"
    }
  }

  // The picture-bearing payload. Text renders from `detailProc` first; this lands later
  // and re-binds the grid. A payload for a show the user has already left is dropped.
  Process {
    id: episodesProc
    stdout: StdioCollector {
      onStreamFinished: {
        var payload
        try {
          payload = JSON.parse(this.text)
        } catch (e) {
          if (root.service)
            root.svc.logEvent(root.episodesTrace || root.svc.newTrace("browse-episodes"),
                              "browse.episodes.parse", { mediaId: root.detailMediaId, error: String(e) })
          return
        }
        if (!payload || typeof payload !== "object" || Number(payload.mediaId) !== root.detailMediaId) return
        root.detailPayload = payload
        if (root.detailCells.length > 0 && root.cellIndex >= root.detailCells.length)
          root.cellIndex = Model.initialCellIndex(root.detailCells)
        // The grid has just re-bound to a different model (the picture-bearing cells replace the
        // number-only ones), and a GridView resets its contentY when its model changes.
        root.positionOnCursor()
      }
    }
    // No UI error on a failure here: the number-only grid (from `detailProc`'s instant
    // `Model.episodeCells`) is the designed degradation. This line is the diagnosis —
    // `gotPayload` says whether the grid ever got pictures for this show at all.
    onExited: function (code) {
      var wanted = root.episodesProcMediaId
      var got = !!root.detailPayload && Number(root.detailPayload.mediaId) === wanted
      // `episodesTrace` is the trace this process was actually launched under, so the exit line
      // joins the CLI's own timeline for that run; a fresh trace here made `omarchy-anime trace`
      // show the exit alone, under a different id, for whichever show happened to be open.
      if (service) root.svc.logEvent(root.episodesTrace || root.svc.newTrace("browse-episodes-exit"),
                                     "browse.episodes.exit",
                                     { mediaId: wanted, code: code, gotPayload: got,
                                       openMediaId: root.detailMediaId })
      // A second show opened while this was running was skipped entirely (`loadEpisodes` refuses
      // to start a second one), so it would never get pictures. Start its fetch now that the slot
      // is free.
      if (root.detailOpen && root.detailMediaId !== wanted) root.loadEpisodes()
    }
  }

  Process {
    id: pinProc
    property int forMedia: 0
    stdout: StdioCollector {
      onStreamFinished: {
        if (pinProc.forMedia !== root.detailMediaId) return
        try {
          var r = JSON.parse(this.text)
          root.detailPin = r && r.pin ? r.pin : null
        } catch (e) {
          root.detailPin = null
        }
      }
    }
  }

  Process {
    id: forgetPinProc
    onExited: function () {
      root.loadPin()
      if (root.pickerOpen && root.service) root.svc.loadReleases(root.pickerMediaId, root.pickerEpisode, false)
    }
  }

  Process {
    id: watchProc
    onExited: function () {
      root.settingWatching = false
      root.loadDetail()
      if (root.service) root.svc.refreshWatching()
    }
  }

  function refreshView() {
    if (!bin) return
    switch (root.view) {
    case "watching":
      // `watching` has a 5-minute cache in the CLI; --refresh rewrites it, then the service
      // re-reads it so both the overlay and the popout see the same list.
      if (!refreshProc.running) {
        refreshProc.command = [bin, "watching", "--refresh"]
        refreshProc.running = true
      }
      break
    case "top": loadTop(true); break
    case "airing": loadAiring(true); break
    case "search": requestSearch(); break
    case "lists": loadLists(true); break
    }
  }

  Process {
    id: refreshProc
    onExited: function () { if (root.service) root.svc.refreshWatching() }
  }

  Process {
    id: listsProc
    // The status this read is for: `listStatus` may change while it runs.
    property string status: ""
    // Set by an AniList account change while this read runs: its answer is the old account's.
    property bool superseded: false
    stdout: StdioCollector {
      onStreamFinished: {
        if (listsProc.superseded) return
        root.listsLoading = false
        var list = null
        try {
          list = JSON.parse(this.text)
        } catch (e) {
          list = null
        }
        var failure = root.logic.listFailure(list, listsProc.status)
        if (failure) {
          // Signed out or a refused token: every status's rows go, they belong to an account this
          // side can no longer read. A network failure keeps what loaded.
          if (root.logic.listFailureDropsRows(failure)) {
            root.listsByStatus = ({})
            root.listsSaved = ({})
          }
          if (root.service && (!root.listsFailure || root.listsFailure.state !== failure.state))
            root.svc.logEvent(root.svc.newTrace("browse-lists"), "ui.list.state",
                              { view: "lists", status: listsProc.status, state: failure.state,
                                cliStage: failure.stage, reason: failure.reason, cliTrace: failure.trace,
                                error: failure.error, dropsRows: root.logic.listFailureDropsRows(failure) })
          root.listsFailure = failure
          return
        }
        // A fresh list, or the saved one AniList's outage answered with: its rows show either way.
        var rows = root.logic.listAnswerRows(list)
        var saved = root.logic.listAnswerStale(list)
        var was = root.listsSaved[listsProc.status] || null
        var shown = root.listsByStatus[listsProc.status]
        // A list answers the failure that applies to its status: its own, or a sign-in state.
        var failed = root.logic.listFailureFor(root.listsFailure, listsProc.status)
        // Logged once per change, as Continue Watching's is: out of a failure, or into or out of the saved list.
        var from = failed ? root.listsFailure.state
                 : !!saved === !!was ? ""
                 : was ? "saved" : !Model.isList(shown) ? "loading" : shown.length > 0 ? "ok" : "empty"
        if (from && root.service)
          root.svc.logEvent(root.svc.newTrace("browse-lists"), "ui.list.state",
                            { view: "lists", status: listsProc.status,
                              state: saved ? "saved" : rows.length > 0 ? "ok" : "empty", from: from,
                              count: rows.length, savedAt: saved ? saved.savedAt : 0,
                              savedReason: saved ? saved.reason : "" })
        if (failed) root.listsFailure = null
        var next = ({})
        for (var k in root.listsByStatus) next[k] = root.listsByStatus[k]
        next[listsProc.status] = rows
        root.listsByStatus = next
        var marks = ({})
        for (var m in root.listsSaved) marks[m] = root.listsSaved[m]
        if (saved) marks[listsProc.status] = saved
        else delete marks[listsProc.status]
        root.listsSaved = marks
        if (listsProc.status === root.listStatus && root.selLists >= rows.length) root.selLists = 0
        root.warmListCovers()
      }
    }
    onExited: function (code) {
      if (code !== 0) root.listsLoading = false
      if (!root.listsQueued) return
      root.listsQueued = false
      root.loadLists(false)
    }
  }

  Process {
    id: topProc
    stdout: StdioCollector {
      onStreamFinished: {
        root.topLoading = false
        var list
        try {
          list = JSON.parse(this.text)
        } catch (e) {
          return
        }
        var answer = root.tabAnswer("top", list)
        if (!answer) return
        root.topSaved = answer.saved
        root.topList = answer.rows
        root.topLoaded = true
        if (root.selTop >= answer.rows.length) root.selTop = 0
        root.warmCovers("top")
      }
    }
    onExited: function (code) { if (code !== 0) root.topLoading = false }
  }

  Process {
    id: airingProc
    stdout: StdioCollector {
      onStreamFinished: {
        root.airingLoading = false
        var list
        try {
          list = JSON.parse(this.text)
        } catch (e) {
          return
        }
        var answer = root.tabAnswer("airing", list)
        if (!answer) return
        root.airingSaved = answer.saved
        root.airingList = answer.rows
        root.airingLoaded = true
        if (root.selAiring >= answer.rows.length) root.selAiring = 0
        root.warmCovers("airing")
      }
    }
    onExited: function (code) { if (code !== 0) root.airingLoading = false }
  }

  Process {
    id: searchProc
    stdout: StdioCollector {
      onStreamFinished: {
        // Stale answers are dropped here, not in onExited: a cancelled process still finishes
        // its stream, and a slower earlier query must never overwrite a newer one.
        if (root.searchExpectedStop || root.searchInFlight !== root.searchSerial) return
        root.searchLoading = false
        var list
        try {
          list = JSON.parse(this.text)
        } catch (e) {
          root.searchList = []
          return
        }
        root.searchList = Model.isList(list) ? list : []
        root.selSearch = 0
        root.warmCovers("search")
      }
    }
    onExited: function () {
      if (!root.searchQueued) return
      root.searchQueued = false
      root.searchExpectedStop = false
      root.startSearch(String(searchField.text).replace(/^\s+|\s+$/g, ""))
    }
  }

  // 400 ms of quiet before a live AniList call.
  Timer {
    id: searchDebounce
    interval: 400
    onTriggered: root.requestSearch()
  }

  SystemClock {
    precision: SystemClock.Minutes
    onDateChanged: root.nowDate = date
  }

  // A play that ends without an error means the player has taken over: get out of the way.
  // `busy` is cleared before `lastError` is written in Service.playProc, so the decision waits
  // one turn for the whole handler to finish.
  Connections {
    target: root.service
    enabled: root.service !== null

    // A device sign-in that saved closes its panel; the row shows the new account.
    function onTorboxDeviceChanged() {
      if (root.service.torboxDevice && root.service.torboxDevice.state === "saved" && root.signingIn === "torbox") {
        root.signingIn = ""
        root.torboxPaste = false
      }
    }

    function onPickerWanted(mediaId, episode) {
      if (!root.opened) return
      root.openPicker(mediaId, episode, false)
    }

    // An AniList sign-in or sign-out. Every status's rows belong to the old account (or
    // to none), so they go; each status re-reads past the CLI cache, the one showing right away.
    // Top 100 and Airing go too: their rows hide the old account's completed shows, and the sign-in deleted the saved
    // copies their line would name. Each reads again on its next entry, the one showing right away.
    function onAnilistAccountChanged() {
      root.topList = []
      root.topSaved = null
      root.topLoaded = false
      root.airingList = []
      root.airingSaved = null
      root.airingLoaded = false
      if (root.view === "top") root.loadTop(false)
      else if (root.view === "airing") root.loadAiring(false)
      var stale = ({})
      for (var i = 0; i < Model.LIST_STATUSES.length; i++) stale[Model.LIST_STATUSES[i]] = true
      root.listsStale = stale
      root.listsByStatus = ({})
      root.listsSaved = ({})
      root.listsFailure = null
      if (listsProc.running) listsProc.superseded = true
      root.svc.logEvent(root.svc.newTrace("browse-lists"), "ui.list.account",
                        { view: root.view, status: root.listStatus, inFlight: listsProc.running })
      if (root.view === "lists") root.loadLists(true)
    }

    // `later` answered the "Tell me" this overlay ran: its words on the status line; recorded, the overlay closes a
    // moment later; not recorded, it stays (the play keeps waiting, its choices still here).
    function onLaterAnswerChanged() {
      var answer = root.service.laterAnswer
      if (!answer || answer.trace !== root.choiceAckTrace) return
      var view = Model.laterAnswerView(answer)
      root.choiceAck = view.text
      if (view.dismiss) choiceAckTimer.restart()
      else choiceAckClear.restart()
    }

    function onBusyChanged() {
      if (!root.playPending || root.service.busy !== "") return
      Qt.callLater(function () {
        if (!root.playPending || !root.service || root.service.busy !== "") return
        root.playPending = false
        // A play that ended holding the viewer's choices stays here: the buttons are on this card. Truthiness, so a
        // Service older than this file (no `playHeld`) still dismisses as before.
        // So does a play refused before it started (an entry that is not out): its sentence is the status line.
        if (root.service.lastError === "" && !root.service.playHeld && !root.service.playRefusal) root.dismiss()
      })
    }
  }

  Timer {
    id: choiceAckClear
    interval: 6000
    onTriggered: root.choiceAck = ""
  }

  Timer {
    id: choiceAckTimer
    interval: 1500
    onTriggered: {
      root.choiceAck = ""
      root.dismiss()
    }
  }

  Timer {
    interval: 1000
    repeat: true
    running: root.opened && !!root.service && !!root.service.playEntry && !!root.service.playEntry.choice
    onTriggered: root.choiceClock = Date.now() / 1000
  }

  // One of the waiting play's choices, from its button or its key: the CLI command the entry gives it, run by the
  // Service (the same command the player's key runs). "Watch the dub now" and "Tell me when it's ready" leave the
  // overlay (the player has the dub; the window is hidden); "Keep waiting" stays, where the wait is shown.
  function runChoice(button) {
    if (!button || !button.runs) return
    var trace = root.svc.runChoice(button)
    root.svc.logEvent(trace || root.svc.newTrace("browse-choice"), "ui.choice.browse",
                      { id: button.id, ran: trace !== "", view: root.view, detail: root.detailOpen })
    if (trace !== "" && button.ack) {
      // "Tell me when it's ready": the status line says what `later` answers (`onLaterAnswerChanged`).
      root.choiceAckTrace = trace
    } else if (trace !== "" && Model.choiceDismisses(button)) root.dismiss()
    // Keeping on waiting from a hold is a play this overlay started: once it ends without an error (or holds again),
    // the overlay gets out of the way as after any play from here (`onBusyChanged`). Keeping on waiting during the
    // wait starts none: the waiting play goes on, and so does this overlay.
    else if (trace !== "" && Model.choiceStartsPlay(button)) root.playPending = true
  }

  // --------------------------------------------------------------------- surface
  PanelWindow {
    id: panel
    // Hidden while Settings is up: a full-screen layer with exclusive focus would cover its window.
    visible: root.opened && !root.settingsOpen
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omaanime-browse"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    Rectangle {
      anchors.fill: parent
      color: root.scrim
    }

    // Click outside the card dismisses, like every Omarchy overlay — except while the picker is open, where "click
    // outside" should mean one level back, not "destroy the overlay I was one keystroke from returning to".
    MouseArea {
      anchors.fill: parent
      acceptedButtons: Qt.AllButtons
      onClicked: {
        if (root.pickerOpen) root.closePicker()
        else root.dismiss()
      }
    }

    BorderSurface {
      id: card
      width: Math.min(Style.space(760), panel.width - Style.gapsOut * 2)
      height: Math.min(Math.round(panel.height * 0.9), panel.height - Style.gapsOut * 2)
      anchors.centerIn: parent
      radius: Style.cornerRadius
      color: root.background
      borderSpec: root.borderSpec
      padding: Style.spacing.panelPadding

      // Swallow clicks that land on the card so the scrim handler above doesn't close it.
      MouseArea { anchors.fill: parent; onClicked: {} }

      Item {
        id: keyCatcher
        anchors.fill: parent
        focus: true

        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function (event) {
          if (root.pickerOpen) {
            switch (event.key) {
            case Qt.Key_Escape:
            case Qt.Key_Backspace:
              root.closePicker()
              event.accepted = true
              return
            case Qt.Key_Down:
            case Qt.Key_J:
              root.pickerMove(1)
              event.accepted = true
              return
            case Qt.Key_Up:
            case Qt.Key_K:
              root.pickerMove(-1)
              event.accepted = true
              return
            case Qt.Key_PageDown:
              root.pickerMove(8)
              event.accepted = true
              return
            case Qt.Key_PageUp:
              root.pickerMove(-8)
              event.accepted = true
              return
            case Qt.Key_Home:
              root.pickerIndex = Math.max(0, Model.pickerFirst(root.pickerItems))
              event.accepted = true
              return
            case Qt.Key_End:
              root.pickerMove(root.pickerItems.length)
              event.accepted = true
              return
            case Qt.Key_Return:
            case Qt.Key_Enter:
              root.pickerPlay()
              event.accepted = true
              return
            case Qt.Key_R:
              root.openPicker(root.pickerMediaId, root.pickerEpisode, true)
              event.accepted = true
              return
            case Qt.Key_X:
              root.forgetPin()
              event.accepted = true
              return
            case Qt.Key_Slash:
              event.accepted = true
              if (!Model.pickerOffersWords(root.pickerData)) return      // no add-on offers text search
              // The last words come back selected, so typing replaces them and Enter repeats them
              // (live 2026-09-26: typing after a pre-fill searched "Glass HarborGlass Harbor").
              pickerField.text = root.pickerData && root.pickerData.query ? root.pickerData.query : ""
              pickerField.forceActiveFocus()
              pickerField.selectAll()
              return
            }
            return
          }
          // The waiting play's choices take their keys first, wherever the card is (but the picker, which owns the
          // keyboard): the status line names them. Only a choice with a command has a key.
          // In another show's detail view the view's keys win (`w` sets that show as watching).
          var chosen = root.choiceKeysHere ? Model.choiceForKey(root.playChoice, event.text) : null
          if (chosen) {
            root.runChoice(chosen)
            event.accepted = true
            return
          }
          if (event.key === Qt.Key_Comma) {
            root.openSettings("browse")
            event.accepted = true
            return
          }
          if (root.detailOpen) {
            switch (event.key) {
            case Qt.Key_Escape:
            case Qt.Key_Backspace:
              root.closeDetail()
              event.accepted = true
              return
            case Qt.Key_Left:
            case Qt.Key_H:
              root.moveCell(-1)
              event.accepted = true
              return
            case Qt.Key_Right:
            case Qt.Key_L:
              root.moveCell(1)
              event.accepted = true
              return
            case Qt.Key_Up:
            case Qt.Key_K:
              root.moveCell(-root.gridColumns)
              event.accepted = true
              return
            case Qt.Key_Down:
            case Qt.Key_J:
              root.moveCell(root.gridColumns)
              event.accepted = true
              return
            case Qt.Key_PageUp:
              root.moveCell(-root.gridColumns * 4)
              event.accepted = true
              return
            case Qt.Key_PageDown:
              root.moveCell(root.gridColumns * 4)
              event.accepted = true
              return
            case Qt.Key_Home:
              root.setCell(0)
              event.accepted = true
              return
            case Qt.Key_End:
              root.setCell(root.detailCells.length - 1)
              event.accepted = true
              return
            case Qt.Key_Return:
            case Qt.Key_Enter:
              root.playCell()
              event.accepted = true
              return
            case Qt.Key_W:
              root.setWatching()
              event.accepted = true
              return
            case Qt.Key_P:
              var pcell = root.detailCells[root.cellIndex]
              if (pcell && pcell.state !== "unaired") root.openPicker(root.detailMediaId, pcell.episode, false)
              event.accepted = true
              return
            case Qt.Key_R:
              root.loadDetail()
              event.accepted = true
              return
            }
            return
          }

          switch (event.key) {
          case Qt.Key_Escape:
            root.dismiss()
            event.accepted = true
            return
          case Qt.Key_E:
            var row = root.currentRow()
            if (row) root.openDetail(row.mediaId)
            event.accepted = true
            return
          case Qt.Key_Tab:
          case Qt.Key_Backtab:
            // Shift+Tab reaches us as Key_Tab with ShiftModifier on this compositor, not as
            // Key_Backtab; Ui/PanelKeyCatcher tests both for the same reason.
            root.cycleView((event.modifiers & Qt.ShiftModifier) || event.key === Qt.Key_Backtab ? -1 : 1)
            event.accepted = true
            return
          case Qt.Key_Left:
          case Qt.Key_H:
            root.cycleView(-1)
            event.accepted = true
            return
          case Qt.Key_Right:
          case Qt.Key_L:
            root.cycleView(1)
            event.accepted = true
            return
          case Qt.Key_Up:
          case Qt.Key_K:
            root.moveSel(-1)
            event.accepted = true
            return
          case Qt.Key_Down:
          case Qt.Key_J:
            root.moveSel(1)
            event.accepted = true
            return
          case Qt.Key_PageUp:
            root.moveSel(-8)
            event.accepted = true
            return
          case Qt.Key_PageDown:
            root.moveSel(8)
            event.accepted = true
            return
          case Qt.Key_Home:
            root.setSel(0)
            event.accepted = true
            return
          case Qt.Key_End:
            root.setSel(root.rows.length - 1)
            event.accepted = true
            return
          case Qt.Key_Return:
          case Qt.Key_Enter:
            root.activate()
            event.accepted = true
            return
          case Qt.Key_R:
            root.refreshView()
            event.accepted = true
            return
          case Qt.Key_BracketLeft:
            if (root.view === "lists") {
              root.cycleListStatus(-1)
              event.accepted = true
            }
            return
          case Qt.Key_BracketRight:
            if (root.view === "lists") {
              root.cycleListStatus(1)
              event.accepted = true
            }
            return
          case Qt.Key_Slash:
            root.focusSearch()
            event.accepted = true
            return
          case Qt.Key_P:
            if (root.service && root.service.lastErrorPicker && root.service.playMediaId > 0) {
              root.openPicker(root.service.playMediaId, root.service.playEpisode, false)
              event.accepted = true
            }
            return
          case Qt.Key_C:
            if (root.view === "top") {
              root.toggleCompleted()
              event.accepted = true
            }
            return
          case Qt.Key_T:
            // The trace id is what makes a failed play diagnosable afterwards
            // (`omarchy-anime trace <id>`); nobody retypes it off a screenshot.
            if (root.service) {
              var copied = root.svc.copyTrace()
              if (copied) {
                root.copiedTrace = copied
                copiedTraceTimer.restart()
              }
              event.accepted = true
            }
            return
          }
        }
      }

      Item {
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset

        // ---------------------------------------------------------------- header
        Column {
          id: head
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          spacing: Style.spacing.md
          visible: !root.detailOpen && !root.pickerOpen

          // Tabs on the left, the keyboard-only actions on the right. Before this phase `r`, `c`
          // and `,` had no pointer route at all; the caption that advertised `c` was a plain Text.
          Item {
            width: parent.width
            height: Math.max(headLeft.height, headActions.height)

            Row {
              id: headLeft
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              anchors.right: headActions.left
              anchors.rightMargin: Style.spacing.md
              spacing: Style.spacing.md

              Text {
                text: "Anime"
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.heading
                anchors.verticalCenter: parent.verticalCenter
              }

              Item { width: Style.space(6); height: 1 }

              Repeater {
                model: root.viewIds

                Button {
                  required property int index
                  required property var modelData
                  anchors.verticalCenter: parent.verticalCenter
                  text: root.viewNames[index]
                  selected: root.view === modelData
                  foreground: root.foreground
                  accent: Commons.Color.accent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.bodySmall
                  onClicked: root.setView(String(modelData))
                }
              }

              // The `c` toggle was a caption that described a key and did nothing when clicked.
              Button {
                anchors.verticalCenter: parent.verticalCenter
                visible: root.view === "top"
                text: root.showCompleted ? "completed shown (c)" : "completed hidden (c)"
                foreground: root.dim
                accent: Commons.Color.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                onClicked: root.toggleCompleted()
              }
            }

            Row {
              id: headActions
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.spacing.xs

              Button {
                anchors.verticalCenter: parent.verticalCenter
                text: "↻"
                tooltipText: "Reload this view (r)"
                foreground: root.foreground
                accent: Commons.Color.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                onClicked: root.refreshView()
              }

              Button {
                anchors.verticalCenter: parent.verticalCenter
                text: "⚙"
                tooltipText: "Settings (,)"
                foreground: root.foreground
                accent: Commons.Color.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                onClicked: root.openSettings("browse")
              }
            }
          }

          // The status picker, only on the Lists tab. `[` and `]` cycle it from the list, and the
          // group's own h/l navigation works once it has focus.
          ButtonGroup {
            width: parent.width
            visible: root.view === "lists"
            options: {
              var out = []
              for (var i = 0; i < Model.LIST_STATUSES.length; i++)
                out.push({ value: Model.LIST_STATUSES[i], label: Model.listStatusLabel(Model.LIST_STATUSES[i]) })
              return out
            }
            value: root.listStatus
            foreground: root.foreground
            background: root.background
            accent: Commons.Color.accent
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            onChanged: function (v) {
              root.setListStatus(String(v))
              Qt.callLater(function () { if (root.opened) keyCatcher.forceActiveFocus() })
            }
          }

          // Search box, only on the Search tab. `/` from any view lands here.
          TextField {
            id: searchField
            width: parent.width
            visible: root.view === "search"
            placeholderText: "Search AniList…"
            foreground: root.foreground
            accent: Commons.Color.accent
            onTextChanged: if (root.view === "search") searchDebounce.restart()

            Keys.priority: Keys.BeforeItem
            Keys.onPressed: function (event) {
              switch (event.key) {
              case Qt.Key_Escape:
                // Escape in the field hands the keyboard back to the list; a second Escape closes.
                keyCatcher.forceActiveFocus()
                event.accepted = true
                return
              case Qt.Key_Down:
                root.moveSel(1)
                event.accepted = true
                return
              case Qt.Key_Up:
                root.moveSel(-1)
                event.accepted = true
                return
              case Qt.Key_PageDown:
                root.moveSel(8)
                event.accepted = true
                return
              case Qt.Key_PageUp:
                root.moveSel(-8)
                event.accepted = true
                return
              case Qt.Key_Return:
              case Qt.Key_Enter:
                root.activate()
                event.accepted = true
                return
              case Qt.Key_Tab:
              case Qt.Key_Backtab:
                root.cycleView((event.modifiers & Qt.ShiftModifier) || event.key === Qt.Key_Backtab ? -1 : 1)
                event.accepted = true
                return
              }
            }
          }
        }

        // ---------------------------------------------------------------- status
        //
        // `t` copies the failed play's trace id, which is the only thing that makes the failure
        // diagnosable afterwards (`omarchy-anime trace <id>`) and nobody retypes off a screenshot.
        // The button appears only while there is an error, because that is the only moment the
        // action means anything.
        // The waiting play's choices, above the status line, as buttons with their keys (`Model.choiceButtonText`),
        // every one live for the whole wait.
        Row {
          id: choiceRow
          anchors.bottom: statusLine.top
          anchors.bottomMargin: Style.spacing.sm
          anchors.left: parent.left
          spacing: Style.spacing.sm
          visible: !!root.playChoice && !root.pickerOpen

          Repeater {
            model: root.playChoice ? root.playChoice.buttons : []

            Button {
              required property var modelData
              text: Model.choiceButtonText(modelData, root.choiceKeysHere)
              enabled: modelData.runs
              bordered: true
              foreground: root.foreground
              accent: Commons.Color.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              onClicked: root.runChoice(modelData)
            }
          }
        }

        Button {
          id: copyTraceButton
          anchors.bottom: parent.bottom
          anchors.right: parent.right
          visible: !!root.service && root.service.lastError !== ""
          text: root.copiedTrace !== "" ? "copied ✓" : "Copy trace (t)"
          bordered: true
          foreground: root.foreground
          accent: Commons.Color.accent
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          onClicked: {
            if (!root.service) return
            var copied = root.svc.copyTrace()
            if (copied) {
              root.copiedTrace = copied
              copiedTraceTimer.restart()
            }
          }
        }

        Text {
          id: statusLine
          anchors.bottom: parent.bottom
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.rightMargin: copyTraceButton.visible ? copyTraceButton.width + Style.spacing.md : 0
          textFormat: Text.PlainText
          wrapMode: Text.Wrap
          // Three lines: an error headline, the detail line under it, and room for a wrap
          // (the flattened one-liner used to be elided into uselessness).
          maximumLineCount: 3
          elide: Text.ElideRight
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          color: root.service && (root.service.lastError !== "" || root.service.notice !== ""
                                  || root.bridge.notice !== "")
                 ? Commons.Color.accent : root.dim
          text: {
            if (!root.service) return "Service not loaded"
            if (root.copiedTrace !== "") return "trace id copied: " + root.copiedTrace
            if (root.choiceAck) return root.choiceAck
            // A play waiting for TorBox (or holding its choices): what TorBox is doing, then the choices' sentence.
            // Before `busy`, which stays "Finding a release…" while this Service's own play runs; after a play's
            // error, the restart notice and the standing notices, which outrank it.
            if (root.playChoice && root.choiceKeysHere && !root.pickerOpen) {
              if (root.service.lastError !== "") return root.svc.errorHeadline() + "\n" + root.svc.errorDetail()
              if (root.bridge.notice !== "") return root.bridge.notice
              if (root.service.notice !== "") return root.service.notice
              return Model.choiceStatusText(root.playChoice)
            }
            // A play asked for meanwhile, and refused, says so under the one that is starting.
            if (root.service.busy !== "")
              return root.service.busy + (root.service.playRefusal ? "\n" + root.service.playRefusal : "")
            // The refusal is the picker's own summary line; down here, the keys.
            if (root.pickerOpen) return Model.pickerKeys(root.pickerData)
            if (root.service.lastError !== "")
              return root.svc.errorHeadline() + "\n" + root.svc.errorDetail()
            // A play refused before it started (an entry that is not out yet): the CLI's sentence.
            if (root.service.playRefusal) return root.service.playRefusal
            // The running Service is older than this file; one command fixes it. After a play's error,
            // as the stale-player notice ranks (it is `service.notice`), and before the standing notices.
            if (root.bridge.notice !== "") return root.bridge.notice
            // A standing notice (token expiry, AniList budget).
            if (root.service.notice !== "") return root.service.notice
            if (root.detailOpen)
              return "Enter plays the selected episode · hjkl moves · p chooses a release · w sets it as watching · Escape goes back"
            if (root.view === "watching")
              return "Enter plays · e opens the episodes · Tab switches view · j/k moves · / searches · r refreshes · Escape closes"
            if (root.view === "top")
              return "Enter opens the episodes · c shows completed · Tab switches view · j/k moves · / searches · Escape closes"
            if (root.view === "lists")
              return "Enter " + (root.listStatus === "PLANNING" ? "starts the show" :
                                 (root.listStatus === "COMPLETED" || root.listStatus === "DROPPED"
                                  ? "opens the episodes" : "resumes"))
                     + " · [ ] switch list · e opens the episodes · Tab switches view · j/k moves · Escape closes"
            return "Enter opens the episodes · Tab switches view · j/k moves · / searches · r refreshes · Escape closes"
          }
        }

        // ------------------------------------------------------------- release picker
        // Every release the episode's search saw: the ones that passed in the ranker's
        // order (the top one is what auto-play would choose), then the rejected ones, dimmed, under
        // one header per reason (BrowseModel.pickerItems). Enter plays exactly the row.
        Column {
          id: pickerHead
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          spacing: Style.spacing.sm
          visible: root.pickerOpen

          Item {
            width: parent.width
            height: Math.max(pickerTitles.height, pickerClose.height)

            Row {
              id: pickerTitles
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.spacing.md

              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "Choose a release"
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.heading
              }

              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "episode " + root.pickerEpisode
                      + (root.pickerData && root.pickerData.from_cache
                         ? " · searched " + Math.max(1, Math.round(Number(root.pickerData.age_s) / 60)) + " min ago"
                         : "")
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            Button {
              id: pickerClose
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: "✕"
              tooltipText: "Back (Esc)"
              foreground: root.foreground
              accent: Commons.Color.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              onClicked: root.closePicker()
            }
          }

          // `/` lands here; Enter searches the words, Escape hands the keyboard back to the list.
          TextField {
            id: pickerField
            width: parent.width
            visible: Model.pickerOffersWords(root.pickerData) && (activeFocus || text !== "")
            placeholderText: "Search your indexers with your own words…"
            foreground: root.foreground
            accent: Commons.Color.accent

            Keys.priority: Keys.BeforeItem
            Keys.onPressed: function (event) {
              switch (event.key) {
              case Qt.Key_Escape:
                pickerField.text = ""
                keyCatcher.forceActiveFocus()
                event.accepted = true
                return
              case Qt.Key_Return:
              case Qt.Key_Enter:
                root.pickerQuery(pickerField.text)
                event.accepted = true
                return
              }
            }
          }

          Text {
            width: parent.width
            text: Model.pickerLine(!!root.service && root.service.pickerLoading,
                                   root.service ? root.service.pickerError : "", root.pickerData)
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            maximumLineCount: 2
            elide: Text.ElideRight
            color: root.pickerData && root.pickerData.refusal ? Commons.Color.accent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        Item {
          id: pickerPane
          anchors.top: pickerHead.bottom
          anchors.topMargin: Style.spacing.md
          anchors.bottom: statusLine.top
          anchors.bottomMargin: Style.spacing.sm
          anchors.left: parent.left
          anchors.right: parent.right
          visible: root.pickerOpen

          ListView {
            id: pickerList
            anchors.fill: parent
            clip: true
            model: root.pickerItems
            currentIndex: root.pickerIndex
            highlightMoveDuration: 90
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; active: true }

            delegate: Item {
              id: pickItem
              required property int index
              required property var modelData

              readonly property bool isRow: modelData.kind === "row"
              readonly property bool current: isRow && index === root.pickerIndex
              width: ListView.view.width
              height: isRow ? Math.round(Style.font.body * 3.6) : Math.round(Style.font.caption * 2.6)

              Rectangle {
                anchors.fill: parent
                anchors.rightMargin: Style.space(2)
                radius: Style.cornerRadius
                visible: pickItem.isRow
                color: pickItem.current ? root.selectedBackground : "transparent"
              }

              MouseArea {
                anchors.fill: parent
                enabled: pickItem.isRow
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onContainsMouseChanged: if (containsMouse) root.pickerIndex = pickItem.index
                onClicked: {
                  root.pickerIndex = pickItem.index
                  root.pickerPlay()
                }
              }

              Text {
                visible: !pickItem.isRow
                anchors.left: parent.left
                anchors.bottom: parent.bottom
                anchors.bottomMargin: Style.space(2)
                text: pickItem.modelData.text || ""
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
              }

              Column {
                visible: pickItem.isRow
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.leftMargin: Style.spacing.sm
                anchors.rightMargin: Style.spacing.md
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(2)
                opacity: pickItem.modelData.dim && !pickItem.current ? 0.6 : 1.0

                Row {
                  width: parent.width
                  spacing: Style.spacing.md

                  Text {
                    id: pickLine
                    text: (pickItem.modelData.pinned ? "📌 " : "") + (pickItem.modelData.line || "")
                    textFormat: Text.PlainText
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                  }

                  Text {
                    width: Math.max(0, parent.width - pickLine.width - Style.spacing.md)
                    horizontalAlignment: Text.AlignRight
                    text: pickItem.modelData.note || ""
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: pickItem.modelData.failed ? Commons.Color.accent : root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }

                Text {
                  width: parent.width
                  text: pickItem.modelData.title || ""
                  textFormat: Text.PlainText
                  elide: Text.ElideRight
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }
          }
        }

        // ------------------------------------------------------------------ list
        Item {
          anchors.top: head.bottom
          anchors.topMargin: Style.spacing.md
          anchors.bottom: choiceRow.visible ? choiceRow.top : statusLine.top
          anchors.bottomMargin: Style.spacing.sm
          anchors.left: parent.left
          anchors.right: parent.right
          visible: !root.detailOpen && !root.pickerOpen

          // The rows are the saved list AniList's outage answered with: one dim line says why and from
          // when, above the rows it is about. Continue Watching's comes from the Service, each Lists status's, Top
          // 100's and Airing's from its own read; the first fresh answer takes it away.
          Text {
            id: savedLine
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: Style.spacing.sm
            visible: text !== "" && root.rows.length > 0
            text: root.savedLineText(root.nowMs)
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          ListView {
            id: list
            anchors.top: savedLine.visible ? savedLine.bottom : parent.top
            anchors.topMargin: savedLine.visible ? Style.spacing.sm : 0
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            clip: true
            model: root.rows
            currentIndex: root.selIndex()
            highlightMoveDuration: 90
            boundsBehavior: Flickable.StopAtBounds
            // Keeps a healthy window of covers alive either side of the fold so scrolling a
            // 100-row list doesn't re-fetch every thumbnail.
            cacheBuffer: root.rowHeight * 8

            // Without this there is no signal at all that a 100-row list continues below the
            // fold. The first-party idiom, attached straight to the view
            // (`plugins/panels/network/Panel.qml:1487`, `bluetooth/Panel.qml:815`), plus
            // `active: true`: Qt fades the handle out unless the view is already moving, which
            // defeats the point — the bar exists to say "there is more below" *before* you scroll.
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; active: true }

            delegate: Item {
              id: rowItem
              required property int index
              required property var modelData

              width: ListView.view.width
              height: root.rowHeight

              readonly property bool current: ListView.isCurrentItem

              // `Ui/CursorSurface.qml:4-8`: an item must NOT read `containsMouse` for colour.
              // Hover moves the shared cursor and the colour derives from that, which is what
              // guarantees a single highlight on screen across keyboard and mouse alike — before
              // this, hovering row 12 while the keyboard was on row 3 painted two.
              Rectangle {
                anchors.fill: parent
                anchors.rightMargin: Style.space(2)
                radius: Style.cornerRadius
                color: rowItem.current ? root.selectedBackground : "transparent"
              }

              MouseArea {
                id: mouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                acceptedButtons: Qt.LeftButton | Qt.RightButton
                onContainsMouseChanged: if (containsMouse) root.setSel(rowItem.index)
                // Left click is Enter. Right click is `e` — the secondary action, the same
                // convention the bar icon already uses (`Panel.qml:84-104`).
                onClicked: function (event) {
                  root.setSel(rowItem.index)
                  if (event.button === Qt.RightButton) root.openDetail(rowItem.modelData.mediaId)
                  else root.activate()
                }
              }

              Row {
                anchors.fill: parent
                anchors.leftMargin: Style.spacing.sm
                // The ⋯ button's lane is reserved on every row, whether or not the button is
                // showing, so the right-hand caption does not shift as the cursor moves.
                anchors.rightMargin: Style.spacing.md + rowMenu.width + Style.space(4)
                spacing: Style.spacing.md

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  visible: text !== ""
                  width: visible ? Style.space(34) : 0
                  horizontalAlignment: Text.AlignRight
                  text: rowItem.modelData.rankText || ""
                  textFormat: Text.PlainText
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }

                Rectangle {
                  anchors.verticalCenter: parent.verticalCenter
                  width: root.coverWidth
                  height: root.coverHeight
                  radius: Style.space(3)
                  color: Util.alpha(root.foreground, 0.08)
                  clip: true

                  Image {
                    anchors.fill: parent
                    // A local file from the covers cache, never a URL.
                    source: root.coverUrl(rowItem.modelData.cover)
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                    cache: true
                    smooth: true
                    // AniList's "medium" cover is ~100 px wide; decoding it at the row size
                    // keeps a hundred of them in the tens of megabytes.
                    sourceSize.width: root.coverWidth * 2
                    visible: status === Image.Ready
                  }
                }

                Column {
                  anchors.verticalCenter: parent.verticalCenter
                  width: parent.width - x - rightText.width - Style.spacing.md
                  spacing: Style.spacing.xxs

                  Text {
                    width: parent.width
                    text: rowItem.modelData.title || ""
                    textFormat: Text.PlainText
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    elide: Text.ElideRight
                  }

                  Text {
                    width: parent.width
                    visible: text !== ""
                    text: rowItem.modelData.caption || ""
                    textFormat: Text.PlainText
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }
                }

                Text {
                  id: rightText
                  anchors.verticalCenter: parent.verticalCenter
                  text: rowItem.modelData.right || ""
                  textFormat: Text.PlainText
                  color: rowItem.current ? root.foreground : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }

              // The one thing a mouse could not do before this phase: reach the episode grid.
              // `e` opens it from the keyboard; a click on a Continue Watching row goes straight
              // to playback, so without this button (and the right-click above) there was no
              // pointer route to the grid from the plugin's main view at all.
              //
              // Declared after the Row so it sits above the row-wide MouseArea and takes the
              // click itself; it therefore also has to move the cursor on hover, because the
              // MouseArea underneath stops seeing the pointer while this one holds it.
              Button {
                id: rowMenu
                anchors.verticalCenter: parent.verticalCenter
                anchors.right: parent.right
                anchors.rightMargin: Style.space(4)
                visible: rowItem.current && !!rowItem.modelData.mediaId
                text: "⋯"
                tooltipText: "Episodes (e)"
                foreground: root.foreground
                accent: Commons.Color.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                horizontalPadding: Style.space(8)
                onHovered: function (on) { if (on) root.setSel(rowItem.index) }
                onClicked: root.openDetail(rowItem.modelData.mediaId)
              }
            }
          }

          // Empty / loading state, shown in place of the rows.
          Text {
            anchors.centerIn: parent
            width: parent.width - Style.space(40)
            horizontalAlignment: Text.AlignHCenter
            visible: root.rows.length === 0
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            text: {
              if (!root.service) return "The Anime service is not loaded"
              switch (root.view) {
              case "top":
                if (root.topLoading) return "Loading the AniList top 100…"
                if (!root.topLoaded) return "Press r to load the top 100"
                return root.showCompleted ? "No entries" : "Everything here is completed — press c to show it"
              case "airing":
                if (root.airingLoading) return "Looking for airing sequels in the top 100…"
                if (!root.airingLoaded) return "Press r to load"
                return "Nothing in the top 100 is airing right now"
              case "search":
                if (root.searchLoading) return "Searching…"
                if (String(searchField.text).replace(/^\s+|\s+$/g, "") === "") return "Type to search AniList"
                return "No results"
              case "lists":
                // An empty AniList list comes back as an *absent* list, not an empty entries array,
                // so "empty" is the normal answer for a status the user has never used — say which
                // list is empty rather than spinning. Signed out, a refused token or offline
                // say so instead of loading forever.
                return root.logic.listStateText({ loading: root.listsLoading,
                                                  loaded: Model.isList(root.listsByStatus[root.listStatus]),
                                                  failure: root.logic.listFailureFor(root.listsFailure,
                                                                                     root.listStatus),
                                                  what: "your " + Model.listStatusLabel(root.listStatus) + " list",
                                                  emptyText: Model.listEmptyText(root.listStatus),
                                                  where: "overlay" }).text
              default:
                // Asked here rather than read off the Service, so it is worded for the overlay.
                return root.logic.listStateText({ loaded: !!root.service && root.service.watchingLoaded === true,
                                                  failure: root.service ? root.service.watchingFailure || null : null,
                                                  what: "your list", emptyText: Model.WATCHING_EMPTY,
                                                  where: "overlay" }).text
              }
            }
          }
        }

        // ---------------------------------------------------------------- detail
        Column {
          id: detailHead
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          spacing: Style.spacing.md
          visible: root.detailOpen && !root.pickerOpen

          Row {
            width: parent.width
            spacing: Style.spacing.md

            Button {
              anchors.verticalCenter: parent.verticalCenter
              text: "← Back"
              foreground: root.foreground
              accent: Commons.Color.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              onClicked: root.closeDetail()
            }

            Button {
              anchors.verticalCenter: parent.verticalCenter
              text: "↻"
              tooltipText: "Reload this show (r)"
              foreground: root.foreground
              accent: Commons.Color.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              // Detail-`r` is `loadDetail()`, not `refreshView()` — the latter reloads the list
              // behind the detail, which is not what the button under "← Back" should do.
              onClicked: root.loadDetail()
            }

            Button {
              anchors.verticalCenter: parent.verticalCenter
              visible: !!root.detailMedia
              text: root.settingWatching ? "Saving…" : "Set as watching (w)"
              bordered: true
              foreground: root.foreground
              accent: Commons.Color.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              onClicked: root.setWatching()
            }
          }

          Row {
            width: parent.width
            spacing: Style.spacing.md
            visible: !!root.detailMedia

            Rectangle {
              width: Style.space(58)
              height: Style.space(84)
              radius: Style.space(3)
              color: Util.alpha(root.foreground, 0.08)
              clip: true

              Image {
                anchors.fill: parent
                source: root.coverUrl(root.detailHeader ? root.detailHeader.cover : "")
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                cache: true
                smooth: true
                sourceSize.width: Style.space(120)
                visible: status === Image.Ready
              }
            }

            Column {
              width: parent.width - Style.space(58) - Style.spacing.md
              spacing: Style.spacing.xs
              anchors.verticalCenter: parent.verticalCenter

              Text {
                width: parent.width
                text: root.detailHeader ? root.detailHeader.title : ""
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                wrapMode: Text.Wrap
                maximumLineCount: 2
                elide: Text.ElideRight
              }

              Text {
                width: parent.width
                text: {
                  if (!root.detailHeader) return ""
                  var parts = []
                  if (root.detailHeader.scoreText) parts.push(root.detailHeader.scoreText)
                  // Not out: "Not out yet", with AniList's date, in place of "Upcoming".
                  if (root.detailHeader.notOutText) parts.push(root.detailHeader.notOutText)
                  else if (root.detailHeader.statusWord) parts.push(root.detailHeader.statusWord)
                  if (root.detailHeader.episodesText) parts.push(root.detailHeader.episodesText)
                  // The note of the cells on screen: the CLI payload's when its cells are shown (they follow the
                  // episode playing), else the grid's own.
                  var cellsNote = root.detailPayload && Model.isList(root.detailPayload.episodes)
                                  && root.detailPayload.episodes.length > 0
                    ? String(root.detailPayload.note || "") : root.detailHeader.cellsNote
                  if (cellsNote) parts.push(cellsNote)
                  return parts.join(" · ")
                }
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }

              Text {
                width: parent.width
                visible: text !== ""
                text: Model.pinLine(root.detailPin)
                textFormat: Text.PlainText
                color: Commons.Color.accent
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }

              Text {
                width: parent.width
                text: root.detailHeader ? root.detailHeader.listState : ""
                textFormat: Text.PlainText
                color: root.detailHeader && root.detailHeader.listState === "Not on your list" ? root.dim
                       : Commons.Color.accent
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }
          }
        }

        Item {
          anchors.top: detailHead.bottom
          anchors.topMargin: Style.spacing.md
          anchors.bottom: choiceRow.visible ? choiceRow.top : statusLine.top
          anchors.bottomMargin: Style.spacing.sm
          anchors.left: parent.left
          anchors.right: parent.right
          visible: root.detailOpen && !root.pickerOpen

          // The only cell grid in the overlay (the list views are rows); one cell per episode,
          // states from BrowseModel.episodeCells.
          GridView {
            id: grid
            anchors.fill: parent
            clip: true
            cellWidth: root.gridColumns > 0 ? Math.floor(width / root.gridColumns) : width
            cellHeight: Math.round(cellWidth * 9 / 16)
            model: root.detailCells
            currentIndex: root.cellIndex
            boundsBehavior: Flickable.StopAtBounds
            cacheBuffer: cellHeight * 4
            visible: root.detailCells.length > 0

            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; active: true }

            delegate: Item {
              id: cellItem
              required property int index
              required property var modelData

              width: grid.cellWidth
              height: grid.cellHeight

              // `root.cellIndex` directly, not `GridView.isCurrentItem`. Measured on this branch
              // (HARBOR LIGHTS, cursor at card 1005): when `episodesProc`'s payload re-binds the model,
              // the GridView resets its own `currentIndex` to 0, and the declarative
              // `currentIndex: root.cellIndex` above does not re-evaluate because `cellIndex` has
              // not changed -- so the cursor silently vanished from the grid until the first arrow
              // key nudged it. `cellIndex` is the cursor everything else already reads (`playCell`,
              // `setCell`, the hover handler), so binding the highlight to it too makes the drawn
              // cursor and the acted-on cursor the same thing by construction.
              readonly property bool current: cellItem.index === root.cellIndex
              readonly property bool playable: modelData.state !== "unaired"
              readonly property var picture: Model.cardPicture(modelData, root.detailShow)
              readonly property real resume: Model.resumeFraction(modelData)

              Rectangle {
                id: card
                anchors.fill: parent
                anchors.margins: Style.space(3)
                radius: Style.space(4)
                clip: true
                color: Qt.darker(root.foreground, 8)
                // The cursor must be unmistakable even on a busy still:
                // the current cell gets a thick, full-accent border regardless of picture; NEXT
                // keeps its thinner accent border when it is not also the current cell.
                border.width: cellItem.current ? Style.space(2)
                            : (cellItem.modelData.state === "next" ? Math.max(1, Style.space(1)) : 0)
                border.color: Commons.Color.accent
                opacity: cellItem.modelData.state === "unaired" ? 0.45 : 1

                Image {
                  anchors.fill: parent
                  source: root.coverUrl(cellItem.picture.source)
                  fillMode: Image.PreserveAspectCrop
                  asynchronous: true
                  cache: true
                  smooth: true
                  sourceSize.width: grid.cellWidth
                  visible: status === Image.Ready
                }

                // The fanart fallback is darkened so the big number reads.
                Rectangle {
                  anchors.fill: parent
                  color: "black"
                  opacity: cellItem.picture.kind === "fanart" ? 0.45 : 0
                }

                Text {
                  anchors.centerIn: parent
                  visible: cellItem.picture.kind !== "still" && cellItem.picture.kind !== "blur"
                  text: String(cellItem.modelData.episode)
                  textFormat: Text.PlainText
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.title * 2
                  font.bold: true
                  color: Util.alpha("white", 0.85)
                }

                // Bottom-up scrim so the title reads on any picture.
                Rectangle {
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.bottom: parent.bottom
                  height: Math.round(parent.height * 0.5)
                  gradient: Gradient {
                    GradientStop { position: 0.0; color: "transparent" }
                    GradientStop { position: 1.0; color: Util.alpha("black", 0.85) }
                  }
                }

                Text {
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.bottom: parent.bottom
                  anchors.margins: Style.space(5)
                  visible: !!cellItem.modelData.title
                  text: cellItem.modelData.title ? String(cellItem.modelData.title) : ""
                  textFormat: Text.PlainText
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  color: "white"
                  elide: Text.ElideRight
                  maximumLineCount: 1
                }

                // Episode number chip, top-left.
                Rectangle {
                  anchors.top: parent.top
                  anchors.left: parent.left
                  anchors.margins: Style.space(4)
                  width: chipText.implicitWidth + Style.space(8)
                  height: chipText.implicitHeight + Style.space(3)
                  radius: Style.space(3)
                  // A second, border-independent cue for the current cell:
                  // a solid accent fill with black text reads even where the card border blends
                  // into a busy still.
                  color: cellItem.current ? Commons.Color.accent : Util.alpha("black", 0.7)
                  border.width: cellItem.modelData.state === "next" ? 1 : 0
                  border.color: Commons.Color.accent

                  Text {
                    id: chipText
                    anchors.centerIn: parent
                    text: String(cellItem.modelData.episode)
                    textFormat: Text.PlainText
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: cellItem.modelData.state === "next" || cellItem.current
                    color: cellItem.current ? "#000000"
                           : (cellItem.modelData.state === "next" ? Commons.Color.accent : "white")
                  }
                }

                // Watched cells keep the check so "crisp" is not the only signal -- inside the
                // same dark chip the episode number uses, because a bare white glyph on a bright
                // still is not a signal at all. Same radius, same padding, same
                // 70%-black fill, so the two corners read as one pair.
                Rectangle {
                  anchors.top: parent.top
                  anchors.right: parent.right
                  anchors.margins: Style.space(4)
                  width: watchedCheck.implicitWidth + Style.space(8)
                  height: watchedCheck.implicitHeight + Style.space(3)
                  radius: Style.space(3)
                  visible: cellItem.modelData.state === "watched"
                  color: Util.alpha("black", 0.7)

                  Text {
                    id: watchedCheck
                    anchors.centerIn: parent
                    text: "✓"
                    textFormat: Text.PlainText
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    color: "white"
                  }
                }

                // Resume bar along the bottom edge.
                Rectangle {
                  anchors.left: parent.left
                  anchors.bottom: parent.bottom
                  height: Style.space(3)
                  width: Math.round(parent.width * cellItem.resume)
                  visible: cellItem.resume > 0
                  color: Commons.Color.accent
                }
              }

              MouseArea {
                id: cellHover
                anchors.fill: parent
                hoverEnabled: true
                enabled: cellItem.playable
                cursorShape: Qt.PointingHandCursor
                onContainsMouseChanged: if (containsMouse) root.setCell(cellItem.index)
                onClicked: {
                  root.setCell(cellItem.index)
                  root.playCell()
                }
              }
            }
          }

          Text {
            anchors.centerIn: parent
            width: parent.width - Style.space(40)
            horizontalAlignment: Text.AlignHCenter
            visible: root.detailCells.length === 0
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: root.detailError !== "" ? Commons.Color.accent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            text: {
              if (root.detailError !== "") return root.detailError
              if (root.detailLoading) return "Loading…"
              return "No episodes to show"
            }
          }
        }
      }
    }
  }

  // ---------------------------------------------------------------- settings window
  // Settings (with Accounts and Advanced) is a normal window, not a sheet on the overlay: the overlay is a
  // full-screen layer with exclusive keyboard focus, so a sign-in that needs the browser (TorBox's code, AniList's
  // token, a Prowlarr or Jackett API key) had to close the plugin to get there. Hyprland can move this one, Alt-Tab
  // reaches it, and it can sit beside the browser while the browser takes the typing. The overlay hides while it is
  // up and comes back when it closes, if it was showing before (`settingsAlone` is false then).
  //
  // It lives in this file, not the Service, because the sheet's state and every control's binding are Browse's; the
  // host keeps this file loaded while `opened` is true (shell.qml's panel Loader, no `keepLoaded`), which it stays
  // while only this window is up. The host loads this file once, whatever the number of screens: one window.
  FloatingWindow {
    id: settingsWindow
    title: "Anime \u00b7 Settings"
    // A FloatingWindow is visible by default; this one is shown and hidden by `settingsShown` alone.
    visible: false
    color: root.background
    implicitWidth: Style.space(760)
    implicitHeight: Math.min(Style.space(860), Math.round((settingsWindow.screen ? settingsWindow.screen.height
                                                                                   : Style.space(960)) * 0.85))
    // A fixed size: Hyprland floats a window whose minimum and maximum are the same (Mixtape's player does this).
    minimumSize: Qt.size(implicitWidth, implicitHeight)
    maximumSize: Qt.size(implicitWidth, implicitHeight)
    // Closed by the compositor (Super+W, a close from the window list): the same as ✕. Not sent on `visible = false`.
    onClosed: root.closeSettings("window")

    Item {
      id: settingsKeys
      anchors.fill: parent
      focus: true

      // The sheet owns the keyboard: Tab walks the controls through Qt's own focus chain and every other key belongs
      // to whichever control has focus. Escape and the scroll keys are handled both here and on settingsPane,
      // because only one of the two has focus at any moment — this item before the first Tab, the pane's control
      // chain after it.
      Keys.onPressed: function (event) {
        switch (event.key) {
        case Qt.Key_Escape:
          root.closeSettings("escape")
          event.accepted = true
          return
        case Qt.Key_PageDown:
        case Qt.Key_Down:
        case Qt.Key_J:
          root.scrollSheet(settingsFlick.height * (event.key === Qt.Key_PageDown ? 0.8 : 0.2))
          event.accepted = true
          return
        case Qt.Key_PageUp:
        case Qt.Key_Up:
        case Qt.Key_K:
          root.scrollSheet(-settingsFlick.height * (event.key === Qt.Key_PageUp ? 0.8 : 0.2))
          event.accepted = true
          return
        case Qt.Key_Home:
          root.scrollSheet(-settingsFlick.contentHeight)
          event.accepted = true
          return
        case Qt.Key_End:
          root.scrollSheet(settingsFlick.contentHeight)
          event.accepted = true
          return
        }
      }

      Item {
        anchors.fill: parent
        anchors.margins: Style.spacing.panelPadding
        // ------------------------------------------------------------- the window's content
        // **Omarchy 4.0.3 has no settings form**: `barWidget.schema` is
        // copied into bar-widget registry metadata and never read again (one assignment at
        // shell.qml:1408; no reader of `defaultValue` anywhere; `Ui/MultiSelect.qml` instantiated by
        // zero files), and `omarchy-menu.jsonc` carries no per-widget settings route. So the plugin
        // draws its own sheet — out of the manifest's own schema, so "what the manifest documents"
        // and "what the sheet renders" stay the same list by construction.
        Column {
          id: settingsHead
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          spacing: Style.spacing.sm
          visible: root.settingsOpen

          Item {
            width: parent.width
            height: Math.max(sheetTitles.height, sheetClose.height)

            // Dragging the header moves the window, as a title bar would (Super+drag does too).
            MouseArea {
              anchors.fill: parent
              anchors.rightMargin: sheetClose.width + Style.spacing.md
              cursorShape: Qt.SizeAllCursor
              onPressed: settingsWindow.startSystemMove()
            }

            Row {
              id: sheetTitles
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.spacing.md

              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "Settings"
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.heading
              }

              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "saved as you change them"
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            // Escape closes the window, and so does this: a mouse needs a way out too (the header was two Texts once).
            Button {
              id: sheetClose
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: "✕"
              tooltipText: root.settingsAlone ? "Close (Esc)" : "Back to the list (Esc)"
              foreground: root.foreground
              accent: Commons.Color.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              onClicked: root.closeSettings("button")
            }
          }
        }

        Item {
          id: settingsPane
          anchors.top: settingsHead.bottom
          anchors.topMargin: Style.spacing.md
          anchors.bottom: settingsStatus.top
          anchors.bottomMargin: Style.spacing.sm
          anchors.left: parent.left
          anchors.right: parent.right
          visible: root.settingsOpen

          readonly property Item focusedControl: settingsPane.Window.activeFocusItem
          onFocusedControlChanged: root.revealInSheet(settingsPane.focusedControl)

          // Escape from inside a control still closes the window: unhandled keys bubble up the focus
          // chain to this ancestor, which settingsKeys no longer sees once a control has focus.
          // PageUp/PageDown/Home/End scroll, because a SpinBox eats Up/Down and there is no
          // guarantee of a pointer.
          Keys.onPressed: function (event) {
            switch (event.key) {
            case Qt.Key_Escape:
              root.closeSettings("escape")
              event.accepted = true
              return
            case Qt.Key_PageDown:
              root.scrollSheet(settingsFlick.height * 0.8)
              event.accepted = true
              return
            case Qt.Key_PageUp:
              root.scrollSheet(-settingsFlick.height * 0.8)
              event.accepted = true
              return
            case Qt.Key_Home:
              root.scrollSheet(-settingsFlick.contentHeight)
              event.accepted = true
              return
            case Qt.Key_End:
              root.scrollSheet(settingsFlick.contentHeight)
              event.accepted = true
              return
            }
          }

          Flickable {
            id: settingsFlick
            anchors.fill: parent
            clip: true
            contentWidth: width
            contentHeight: settingsColumn.height
            boundsBehavior: Flickable.StopAtBounds

            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; active: true }

            Column {
              id: settingsColumn
              // A lane for the scrollbar, so a row's trailing button is never under the handle.
              width: settingsFlick.width - Style.space(10)
              spacing: Style.spacing.md

              // ---------------------------------------------------------------- Accounts
              Text {
                width: parent.width
                text: "Accounts"
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.subtitle
              }

              Text {
                width: parent.width
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: root.service && root.service.accountsError !== ""
                      ? root.service.accountsError
                      : "A token is stored at ~/.config/omaanime/<provider>-token, mode 600, "
                        + "and never anywhere else — not in Omarchy's shell.json, not in the log, "
                        + "not on a command line."
              }

              Repeater {
                model: root.accountProviders

                Column {
                  id: acctRow
                  required property var modelData
                  readonly property string provider: String(modelData)
                  readonly property var state: root.account(provider)
                  // `present` is absent until `accounts` has answered, and `visible` is a bool.
                  readonly property bool connected: acctRow.state.present === true
                  width: settingsColumn.width
                  spacing: Style.space(4)

                  Item {
                    width: parent.width
                    height: Math.max(acctText.height, acctActions.height)

                    Column {
                      id: acctText
                      anchors.left: parent.left
                      anchors.right: acctActions.left
                      anchors.rightMargin: Style.spacing.md
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: Style.space(1)

                      Text {
                        text: root.accountLabels[acctRow.provider] || acctRow.provider
                        textFormat: Text.PlainText
                        color: root.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.body
                      }

                      Text {
                        width: parent.width
                        text: root.accountLine(acctRow.provider)
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                        color: root.accountColour(acctRow.provider)
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                      }
                    }

                    Row {
                      id: acctActions
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: Style.space(6)

                      Button {
                        anchors.verticalCenter: parent.verticalCenter
                        focusable: true
                        // No Cancel while TorBox's token is being checked and saved.
                        visible: (!acctRow.connected || root.signingIn === acctRow.provider)
                                 && !(acctRow.provider === "torbox" && root.signingIn === "torbox"
                                      && !root.logic.deviceCancellable(root.torboxDevice))
                        text: root.signingIn === acctRow.provider ? "Cancel" : "Sign in"
                        bordered: true
                        foreground: root.foreground
                        accent: Commons.Color.accent
                        fontFamily: root.fontFamily
                        fontSize: Style.font.caption
                        onClicked: {
                          if (root.signingIn === acctRow.provider) root.closeSignIn()
                          else root.openSignIn(acctRow.provider)
                        }
                      }

                      Button {
                        anchors.verticalCenter: parent.verticalCenter
                        focusable: true
                        visible: acctRow.connected && root.signingIn !== acctRow.provider
                        text: "Replace"
                        tooltipText: acctRow.provider === "torbox" ? "Sign in again with a code"
                                                                   : "Paste a new token for this provider"
                        foreground: root.foreground
                        accent: Commons.Color.accent
                        fontFamily: root.fontFamily
                        fontSize: Style.font.caption
                        onClicked: root.openSignIn(acctRow.provider)
                      }

                      // Two clicks, because this button sits next to twenty-three settings
                      // controls. The confirm times out back to the first state after 5 s.
                      Button {
                        anchors.verticalCenter: parent.verticalCenter
                        focusable: true
                        visible: acctRow.connected && root.signingIn !== acctRow.provider
                        text: root.confirmingOut === acctRow.provider ? "Really sign out?" : "Sign out"
                        bordered: root.confirmingOut === acctRow.provider
                        foreground: root.confirmingOut === acctRow.provider ? Commons.Color.accent : root.foreground
                        accent: Commons.Color.accent
                        fontFamily: root.fontFamily
                        fontSize: Style.font.caption
                        onClicked: {
                          if (root.confirmingOut === acctRow.provider) {
                            root.confirmingOut = ""
                            if (root.service) root.svc.signOut(acctRow.provider)
                          } else {
                            root.confirmingOut = acctRow.provider
                            confirmTimer.restart()
                          }
                        }
                      }
                    }
                  }

                  // TorBox's device-code sign-in, in place. The code and the short URL are
                  // the whole instruction; the QR code (from qrencode, an Omarchy base package)
                  // opens the same approval page on a phone. Neither the device code nor the token
                  // is ever in this sheet: the CLI does not print them.
                  Row {
                    width: parent.width
                    spacing: Style.space(10)
                    visible: acctRow.provider === "torbox" && root.signingIn === "torbox" && !root.torboxPaste

                    // A light square with its quiet zone, whatever the theme, or scanners fail.
                    Rectangle {
                      id: qrBox
                      readonly property var rows: root.torboxDevice.qr || []
                      readonly property int size: rows.length
                      readonly property int module: size > 0 ? Math.max(2, Math.floor(Style.space(120) / size)) : 0
                      visible: size > 0 && root.torboxDevice.state === "waiting"
                      width: size * module
                      height: size * module
                      color: "#ffffff"

                      Grid {
                        columns: qrBox.size
                        Repeater {
                          model: qrBox.visible ? qrBox.size * qrBox.size : 0
                          Rectangle {
                            required property int index
                            width: qrBox.module
                            height: qrBox.module
                            color: String(qrBox.rows[Math.floor(index / qrBox.size)]).charAt(index % qrBox.size) === "1"
                                   ? "#111111" : "transparent"
                          }
                        }
                      }
                    }

                    Column {
                      width: parent.width - (qrBox.visible ? qrBox.width + Style.space(10) : 0)
                      spacing: Style.space(4)

                      Text {
                        width: parent.width
                        visible: root.torboxDevice.state === "waiting"
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                        color: root.dim
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        text: "Open " + (root.torboxDevice.url || "the page") + " and enter this code:"
                      }

                      Text {
                        visible: root.torboxDevice.state === "waiting" && !!root.torboxDevice.code
                        textFormat: Text.PlainText
                        color: Commons.Color.accent
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.title
                        font.letterSpacing: Style.space(2)
                        text: String(root.torboxDevice.code || "")
                      }

                      // The Service put the code on the clipboard when it showed (`copyDeviceCode`).
                      Text {
                        width: parent.width
                        visible: root.torboxDevice.state === "waiting" && !!root.torboxDevice.code && text !== ""
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                        color: root.service && root.service.torboxCodeClip === "failed" ? Commons.Color.accent
                                                                                       : root.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        text: Model.deviceClipText(root.service ? root.service.torboxCodeClip : "")
                      }

                      Text {
                        width: parent.width
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                        color: root.torboxDevice.state === "expired" || root.torboxDevice.state === "denied"
                               || root.torboxDevice.state === "failed" ? Commons.Color.accent : root.dim
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        text: root.logic.deviceStatusText(root.torboxDevice)
                      }

                      Row {
                        spacing: Style.space(6)

                        Button {
                          focusable: true
                          visible: root.torboxDevice.state === "waiting" && !!root.torboxDevice.verificationUrl
                          text: "Open page"
                          tooltipText: String(root.torboxDevice.url || "")
                          bordered: true
                          foreground: root.foreground
                          accent: Commons.Color.accent
                          fontFamily: root.fontFamily
                          fontSize: Style.font.caption
                          onClicked: Quickshell.execDetached(["xdg-open", String(root.torboxDevice.verificationUrl)])
                        }

                        Button {
                          focusable: true
                          visible: root.torboxDevice.state === "expired" || root.torboxDevice.state === "denied"
                                   || root.torboxDevice.state === "failed" || root.torboxDevice.state === "cancelled"
                          text: "Get a new code"
                          bordered: true
                          foreground: root.foreground
                          accent: Commons.Color.accent
                          fontFamily: root.fontFamily
                          fontSize: Style.font.caption
                          onClicked: if (root.service) root.svc.signInTorBoxDevice()
                        }

                        Button {
                          focusable: true
                          visible: root.logic.deviceCancellable(root.torboxDevice)
                          text: "Paste a token instead"
                          tooltipText: "The API token from torbox.app/settings"
                          foreground: root.dim
                          accent: Commons.Color.accent
                          fontFamily: root.fontFamily
                          fontSize: Style.font.caption
                          onClicked: root.pasteTorBoxInstead()
                        }
                      }
                    }
                  }

                  // The paste row, in place, only for the provider being signed in to. TorBox's
                  // shows once "Paste a token instead" is chosen.
                  Row {
                    width: parent.width
                    spacing: Style.space(6)
                    visible: root.signingIn === acctRow.provider && (acctRow.provider !== "torbox" || root.torboxPaste)

                    Button {
                      id: openPage
                      anchors.verticalCenter: parent.verticalCenter
                      focusable: true
                      text: "Open page"
                      tooltipText: acctRow.state.signInUrl || ""
                      bordered: true
                      foreground: root.foreground
                      accent: Commons.Color.accent
                      fontFamily: root.fontFamily
                      fontSize: Style.font.caption
                      // The URL comes from `accounts`, so the AniList client id is never rebuilt here.
                      onClicked: if (acctRow.state.signInUrl)
                        Quickshell.execDetached(["xdg-open", String(acctRow.state.signInUrl)])
                    }

                    TextField {
                      id: tokenField
                      anchors.verticalCenter: parent.verticalCenter
                      // Measured from the buttons rather than a guessed constant: a hardcoded
                      // reserve left "Clear clipboard" half off the card.
                      width: Math.max(Style.space(80), parent.width - openPage.width - saveToken.width
                                      - clearClip.width - Style.space(30))
                      placeholderText: "paste the token"
                      // Dots, not characters — and the value is never read into any log statement.
                      echoMode: TextInput.Password
                      foreground: root.foreground
                      accent: Commons.Color.accent
                      font.family: root.fontFamily
                      onAccepted: saveToken.clicked()
                    }

                    Button {
                      id: saveToken
                      anchors.verticalCenter: parent.verticalCenter
                      focusable: true
                      text: "Save"
                      bordered: true
                      enabled: tokenField.text.length > 0
                      opacity: enabled ? 1 : 0.35
                      foreground: root.foreground
                      accent: Commons.Color.accent
                      fontFamily: root.fontFamily
                      fontSize: Style.font.caption
                      onClicked: {
                        if (tokenField.text.length === 0 || !root.service) return
                        // Cleared only once the Service took it: a refused token stays for the next Save.
                        var taken = root.svc.signIn(acctRow.provider, tokenField.text)
                        if (taken !== true) {
                          // The waiting note is for a busy refusal only; the Service says any other reason itself.
                          root.signInWaiting = taken === "busy" ? acctRow.provider : ""
                          return
                        }
                        root.signInWaiting = ""
                        tokenField.text = ""            // consumed; gone from the field at once
                        root.signingIn = ""
                        root.torboxPaste = false
                      }
                    }

                    Button {
                      id: clearClip
                      anchors.verticalCenter: parent.verticalCenter
                      focusable: true
                      text: "Clear clipboard"
                      tooltipText: "wl-copy --clear — the token is probably still on it"
                      foreground: root.dim
                      accent: Commons.Color.accent
                      fontFamily: root.fontFamily
                      fontSize: Style.font.caption
                      onClicked: if (root.service) root.svc.clearClipboard()
                    }
                  }

                  Text {
                    width: parent.width
                    visible: root.signInWaiting === acctRow.provider && root.signingIn === acctRow.provider
                    text: Model.SIGN_IN_WAITING
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: Commons.Color.accent
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
              }

              // ---------------------------------------------------------------------- Torznab servers
              // Your own Prowlarr or Jackett: add (address and API key, checked before it is saved), check, and
              // remove (two clicks, like Sign out). The key field shows dots and is emptied the moment it is sent;
              // a row shows at most the key's last four characters, as the CLI printed them.
              Item { width: 1; height: Style.spacing.sm }

              Item {
                width: parent.width
                height: Math.max(torznabHeading.height, torznabAddButton.height)

                Text {
                  id: torznabHeading
                  anchors.left: parent.left
                  anchors.verticalCenter: parent.verticalCenter
                  text: "Torznab servers"
                  textFormat: Text.PlainText
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }

                Button {
                  id: torznabAddButton
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  focusable: true
                  visible: !root.torznabAdding
                  enabled: root.torznabBusy === ""
                  opacity: enabled ? 1 : 0.35
                  text: "Add a server"
                  tooltipText: "Your Prowlarr (its own address) or a Jackett or other Torznab feed URL, "
                               + "with its API key"
                  bordered: true
                  foreground: root.foreground
                  accent: Commons.Color.accent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  onClicked: {
                    root.torznabAdding = true
                    root.torznabConfirm = ""
                    if (root.service && "torznabAnswer" in root.service) root.service.torznabAnswer = null
                  }
                }
              }

              Text {
                width: parent.width
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                color: root.service && root.service.accounts && root.service.accounts.torznab
                       && (root.service.accounts.torznab.problem
                           || (root.service.accounts.torznab.skipped || []).length > 0)
                          ? Commons.Color.accent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: root.torznabSection.caption
                visible: text !== ""
              }

              // A Prowlarr or Jackett found running on this computer and not listed yet. The button
              // opens the add row with its address filled in; the user pastes the key, and Add and check does the rest.
              Repeater {
                model: root.torznabOffers

                Item {
                  id: torznabOffer
                  required property var modelData
                  visible: !root.torznabAdding
                  width: parent.width
                  height: Math.max(torznabOfferText.height, torznabOfferUse.height)

                  Column {
                    id: torznabOfferText
                    anchors.left: parent.left
                    anchors.right: torznabOfferUse.left
                    anchors.rightMargin: Style.space(8)
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 2

                    Text {
                      width: parent.width
                      wrapMode: Text.Wrap
                      textFormat: Text.PlainText
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      text: String(torznabOffer.modelData.text)
                    }

                    Text {
                      width: parent.width
                      wrapMode: Text.Wrap
                      textFormat: Text.PlainText
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      text: String(torznabOffer.modelData.where)
                      visible: text !== ""
                    }
                  }

                  Button {
                    id: torznabOfferUse
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    focusable: true
                    enabled: root.torznabBusy === ""
                    opacity: enabled ? 1 : 0.35
                    text: "Add " + String(torznabOffer.modelData.product)
                    tooltipText: String(torznabOffer.modelData.address)
                    bordered: true
                    foreground: root.foreground
                    accent: Commons.Color.accent
                    fontFamily: root.fontFamily
                    fontSize: Style.font.caption
                    onClicked: {
                      if (root.service && "torznabAnswer" in root.service) root.service.torznabAnswer = null
                      root.torznabConfirm = ""
                      torznabUrlField.text = String(torznabOffer.modelData.address)
                      torznabKeyField.text = ""
                      root.torznabAdding = true
                      Qt.callLater(function () { torznabKeyField.forceActiveFocus() })
                    }
                  }
                }
              }

              // The add row: address, key (dots), Add and check, Cancel. The check runs in the Service's own
              // process, so the sheet stays live while a server that does not answer is waited on.
              Row {
                width: parent.width
                spacing: Style.space(6)
                visible: root.torznabAdding

                TextField {
                  id: torznabUrlField
                  anchors.verticalCenter: parent.verticalCenter
                  width: Math.max(Style.space(80), (parent.width - torznabSave.width - torznabCancel.width
                                                    - Style.space(24)) * 0.55)
                  placeholderText: "http://host:9696, or a Torznab feed URL"
                  foreground: root.foreground
                  accent: Commons.Color.accent
                  font.family: root.fontFamily
                  onAccepted: torznabKeyField.forceActiveFocus()
                }

                TextField {
                  id: torznabKeyField
                  anchors.verticalCenter: parent.verticalCenter
                  width: Math.max(Style.space(60), parent.width - torznabUrlField.width - torznabSave.width
                                  - torznabCancel.width - Style.space(24))
                  placeholderText: "API key"
                  // Dots, not characters — and the value is never read into any log statement.
                  echoMode: TextInput.Password
                  foreground: root.foreground
                  accent: Commons.Color.accent
                  font.family: root.fontFamily
                  onAccepted: torznabSave.clicked()
                }

                Button {
                  id: torznabSave
                  anchors.verticalCenter: parent.verticalCenter
                  focusable: true
                  text: "Add and check"
                  bordered: true
                  enabled: torznabUrlField.text.trim().length > 0 && root.torznabBusy === ""
                  opacity: enabled ? 1 : 0.35
                  foreground: root.foreground
                  accent: Commons.Color.accent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  onClicked: {
                    if (!enabled || !root.service) return
                    if (root.svc.addTorznab(torznabUrlField.text, torznabKeyField.text)) {
                      torznabKeyField.text = ""       // consumed; gone from the field at once
                      torznabUrlField.text = ""
                      root.torznabAdding = false
                    }
                  }
                }

                Button {
                  id: torznabCancel
                  anchors.verticalCenter: parent.verticalCenter
                  focusable: true
                  text: "Cancel"
                  foreground: root.dim
                  accent: Commons.Color.accent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  onClicked: {
                    torznabKeyField.text = ""
                    torznabUrlField.text = ""
                    root.torznabAdding = false
                  }
                }
              }

              Text {
                width: parent.width
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                color: root.torznabBusy === "add" ? root.dim
                       : root.service && root.service.torznabAnswer && root.service.torznabAnswer.saved !== true
                         && root.service.torznabAnswer.removed !== false ? Commons.Color.accent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: root.torznabBusy === "add" ? "Checking the server before saving it (" + root.torznabWait + ")…"
                      : root.service ? Model.torznabResultText(root.service.torznabAnswer) : ""
                visible: text !== ""
              }

              Repeater {
                model: root.torznabSection.rows

                Column {
                  id: torznabRow
                  required property var modelData
                  readonly property string rowId: String(modelData.id)
                  readonly property bool checking: root.torznabBusy === "check:" + rowId
                  width: settingsColumn.width
                  spacing: Style.space(2)

                  Item {
                    width: parent.width
                    height: Math.max(torznabText.height, torznabActions.height)

                    Column {
                      id: torznabText
                      anchors.left: parent.left
                      anchors.right: torznabActions.left
                      anchors.rightMargin: Style.spacing.md
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: Style.space(1)

                      Text {
                        width: parent.width
                        text: String(torznabRow.modelData.title)
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                        color: root.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.body
                      }

                      Text {
                        width: parent.width
                        text: String(torznabRow.modelData.line)
                        textFormat: Text.PlainText
                        wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                        color: torznabRow.modelData.bad ? Commons.Color.accent : root.dim
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                      }
                    }

                    Row {
                      id: torznabActions
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: Style.space(6)

                      Button {
                        anchors.verticalCenter: parent.verticalCenter
                        focusable: true
                        enabled: root.torznabBusy === ""
                        opacity: enabled || torznabRow.checking ? 1 : 0.35
                        text: torznabRow.checking ? "Checking…" : "Check"
                        tooltipText: "Ask the server again what it is and which feeds it has"
                        foreground: root.foreground
                        accent: Commons.Color.accent
                        fontFamily: root.fontFamily
                        fontSize: Style.font.caption
                        onClicked: if (root.service) root.svc.checkTorznab(torznabRow.rowId, torznabRow.modelData.row)
                      }

                      Button {
                        anchors.verticalCenter: parent.verticalCenter
                        focusable: true
                        enabled: root.torznabBusy === ""
                        opacity: enabled ? 1 : 0.35
                        text: root.torznabConfirm === torznabRow.rowId ? "Really remove?" : "Remove"
                        bordered: root.torznabConfirm === torznabRow.rowId
                        foreground: root.torznabConfirm === torznabRow.rowId ? Commons.Color.accent : root.foreground
                        accent: Commons.Color.accent
                        fontFamily: root.fontFamily
                        fontSize: Style.font.caption
                        onClicked: {
                          if (root.torznabConfirm === torznabRow.rowId) {
                            root.torznabConfirm = ""
                            if (root.service) root.svc.removeTorznab(torznabRow.rowId, torznabRow.modelData.row)
                          } else {
                            root.torznabConfirm = torznabRow.rowId
                            torznabConfirmTimer.restart()
                          }
                        }
                      }
                    }
                  }

                  Text {
                    width: parent.width
                    visible: text !== ""
                    wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                    color: torznabRow.checking ? root.dim
                           : torznabRow.modelData.checkBad ? Commons.Color.accent : root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    text: torznabRow.checking ? "Checking (" + root.torznabWait + ")…"
                                              : String(torznabRow.modelData.check)
                  }

                  Repeater {
                    model: torznabRow.checking ? [] : torznabRow.modelData.checkLines

                    Text {
                      required property var modelData
                      width: torznabRow.width
                      wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                      textFormat: Text.PlainText
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      text: "· " + String(modelData)
                    }
                  }
                }
              }

              // ---------------------------------------------------------------- Indexers
              // The rows are text only; the reason a file did not load wraps, as it is the part a viewer has to read to
              // fix it. The two AI agent buttons after them are the section's only stops for Tab.
              Item { width: 1; height: Style.spacing.sm }

              Text {
                width: parent.width
                text: "Indexers"
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.subtitle
              }

              Text {
                width: parent.width
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: root.indexerSection.caption !== "" ? root.indexerSection.caption
                      : !root.service ? ""
                      : root.service.accountsLoading ? "reading…"
                      : root.service.accountsError !== ""
                        ? "The add-on status could not be read: " + root.service.accountsError
                      : ""
                visible: text !== ""
              }

              Repeater {
                model: root.indexerSection.rows

                Column {
                  id: indexerRow
                  required property var modelData
                  // A loaded add-on reads plainly; a file that did not load, or a folder problem, in accent.
                  readonly property bool ok: modelData.kind === "loaded"
                  width: settingsColumn.width
                  spacing: Style.space(1)

                  Text {
                    width: parent.width
                    text: String(indexerRow.modelData.title)
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: indexerRow.ok ? root.foreground : Commons.Color.accent
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                  }

                  Text {
                    width: parent.width
                    text: String(indexerRow.modelData.detail)
                    textFormat: Text.PlainText
                    wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                    color: indexerRow.ok ? root.dim : Commons.Color.accent
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
              }

              Text {
                width: parent.width
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: Model.AGENT_CAPTION
              }

              Repeater {
                model: root.agentRows

                Column {
                  id: agentRow
                  required property var modelData
                  width: settingsColumn.width
                  spacing: Style.space(1)

                  Button {
                    focusable: true
                    text: String(agentRow.modelData.label)
                    bordered: true
                    foreground: root.foreground
                    accent: Commons.Color.accent
                    fontFamily: root.fontFamily
                    fontSize: Style.font.caption
                    onClicked: root.svc.askAgent(String(agentRow.modelData.kind))
                  }

                  Text {
                    width: parent.width
                    text: String(agentRow.modelData.detail)
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Text {
                    width: parent.width
                    text: String(agentRow.modelData.note)
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    visible: text !== ""
                  }
                }
              }

              Item { width: 1; height: Style.spacing.sm }

              Text {
                width: parent.width
                text: "Playback"
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.subtitle
              }

              Repeater {
                model: root.sheetRows

                Column {
                  id: settingRow
                  required property var modelData
                  readonly property string rowKey: String(modelData.key || "")
                  readonly property string rowSource: root.settingSource(rowKey)
                  width: settingsColumn.width
                  spacing: Style.space(2)

                  // One control per schema type. `boolean` is a Toggle (stateless — it emits
                  // clicked() and we flip the value ourselves); anything with `options` is a
                  // ButtonGroup up to three choices and a Dropdown beyond that; a bare `integer` is
                  // a NumberField bounded by the schema's own min/max/step; a bare `string` is a
                  // text field.
                  Loader {
                    width: parent.width
                    sourceComponent: {
                      if (settingRow.modelData.type === "boolean") return toggleControl
                      if (settingRow.modelData.type === "list") return listControl
                      var opts = settingRow.modelData.options
                      if (opts && opts.length > 0) return opts.length > 3 ? dropdownControl : groupControl
                      if (settingRow.modelData.type === "integer") return numberControl
                      return textControl
                    }
                    onLoaded: item.row = settingRow.modelData
                  }

                  // Where this value comes from, and a way back. Shown only when it is not the
                  // default, so the sheet stays quiet until precedence actually matters.
                  Row {
                    spacing: Style.spacing.sm
                    visible: settingRow.rowSource !== "default"

                    Text {
                      anchors.verticalCenter: parent.verticalCenter
                      text: root.sourceCaption(settingRow.rowKey)
                      textFormat: Text.PlainText
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                    }

                    Button {
                      anchors.verticalCenter: parent.verticalCenter
                      visible: settingRow.rowSource === "shell"
                      text: "reset"
                      tooltipText: "Drop this key from shell.json, back to config.json or the default"
                      foreground: root.foreground
                      accent: Commons.Color.accent
                      fontFamily: root.fontFamily
                      fontSize: Style.font.caption
                      onClicked: root.resetSetting(settingRow.rowKey)
                    }
                  }
                }
              }

              Item { width: 1; height: Style.spacing.md }

              // The disclosure header: clickable, Enter-able, and it says how many of the keys
              // behind it are not at their default so a changed one is never hidden silently.
              Row {
                id: advancedHead
                width: parent.width
                spacing: Style.spacing.sm

                Button {
                  id: advancedToggle
                  anchors.verticalCenter: parent.verticalCenter
                  focusable: true
                  text: (root.advancedOpen ? "\u25be  " : "\u25b8  ") + "Advanced \u00b7 "
                        + root.advancedRows.length + " settings"
                  tooltipText: "Indexer and provider order, fansub groups, budgets. Same store, same precedence."
                  foreground: root.foreground
                  accent: Commons.Color.accent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.bodySmall
                  onClicked: root.advancedOpen = !root.advancedOpen
                  Keys.onPressed: function (event) {
                    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter
                        || event.key === Qt.Key_Space) {
                      root.advancedOpen = !root.advancedOpen
                      event.accepted = true
                    }
                  }
                }

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  visible: !root.advancedOpen && root.advancedChangedCount() > 0
                  text: root.advancedChangedCount() + " changed"
                  textFormat: Text.PlainText
                  color: Commons.Color.accent
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }

                Button {
                  anchors.verticalCenter: parent.verticalCenter
                  visible: root.advancedOpen && root.advancedChangedCount() > 0
                  text: "reset all"
                  tooltipText: "Drop every advanced key from shell.json in one write"
                  foreground: root.foreground
                  accent: Commons.Color.accent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  onClicked: root.resetAdvanced()
                }
              }

              Text {
                width: parent.width
                visible: root.advancedOpen
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                text: "Saved to Omarchy's shell.json like every other setting. "
                      + (root.service ? root.service.configPath : "config.json")
                      + " is still the middle layer: a key set there wins over the default and loses "
                      + "to anything saved here, and reset hands the key back to it."
              }

              Repeater {
                model: root.advancedOpen ? root.advancedRows : []

                Column {
                  id: advancedRow
                  required property var modelData
                  readonly property string rowKey: String(modelData.key || "")
                  readonly property string rowSource: root.settingSource(rowKey)
                  width: settingsColumn.width
                  spacing: Style.space(2)

                  Loader {
                    width: parent.width
                    sourceComponent: {
                      if (advancedRow.modelData.type === "boolean") return toggleControl
                      if (advancedRow.modelData.type === "list") return listControl
                      var opts = advancedRow.modelData.options
                      if (opts && opts.length > 0) return opts.length > 3 ? dropdownControl : groupControl
                      if (advancedRow.modelData.type === "integer") return numberControl
                      return textControl
                    }
                    onLoaded: item.row = advancedRow.modelData
                  }

                  Row {
                    spacing: Style.spacing.sm
                    visible: advancedRow.rowSource !== "default"

                    Text {
                      anchors.verticalCenter: parent.verticalCenter
                      text: root.sourceCaption(advancedRow.rowKey)
                      textFormat: Text.PlainText
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                    }

                    Button {
                      anchors.verticalCenter: parent.verticalCenter
                      visible: advancedRow.rowSource === "shell"
                      text: "reset"
                      tooltipText: "Drop this key from shell.json, back to config.json or the default"
                      foreground: root.foreground
                      accent: Commons.Color.accent
                      fontFamily: root.fontFamily
                      fontSize: Style.font.caption
                      onClicked: root.resetSetting(advancedRow.rowKey)
                    }
                  }
                }
              }

              Item { width: 1; height: Style.spacing.md }
            }
          }
        }

        // --- the control shapes the schema maps onto ------------------------------------
        Component {
          id: listControl
          Column {
            id: listCol
            property var row: ({})
            width: parent ? parent.width : 0
            spacing: Style.space(4)

            Text {
              text: String(listCol.row.label || listCol.row.key || "")
              textFormat: Text.PlainText
              color: Qt.darker(root.foreground, 1.4)
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            OrderedList {
              width: parent.width
              values: root.settingValue(String(listCol.row.key || ""))
              options: listCol.row.options ? listCol.row.options : []
              foreground: root.foreground
              dim: root.dim
              fontFamily: root.fontFamily
              readonly property var shape: Model.listRowShape(String(listCol.row.key || ""))
              placeholder: shape.placeholder
              addLabel: shape.addLabel
              // Free-text rows check their entries per key (add-on names, group names);
              // the fixed-option list needs no validator.
              validator: (listCol.row.options && listCol.row.options.length > 0) ? null : shape.validator
              // Stored comma-joined, because a JSON array does not survive the shell.json entry
              // (measured; `core.as_list` is the Python half).
              onChanged: function (values) {
                root.writeSetting(String(listCol.row.key || ""), Model.joinList(values))
              }
            }

            Text {
              width: parent.width
              visible: text !== ""
              wrapMode: Text.Wrap
              textFormat: Text.PlainText
              text: String(listCol.row.description || "")
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }

        Component {
          id: toggleControl
          Toggle {
            property var row: ({})
            width: parent ? parent.width : 0
            label: String(row.label || row.key || "")
            description: String(row.description || "")
            checked: root.logic.asBool(root.settingValue(String(row.key || "")), false)   // "true" too
            foreground: root.foreground
            accent: Commons.Color.accent
            fontFamily: root.fontFamily
            // Ui/Toggle is stateless: it reports the click and we decide the new value.
            onClicked: root.writeSetting(String(row.key || ""), !checked)
          }
        }

        Component {
          id: groupControl
          Column {
            property var row: ({})
            width: parent ? parent.width : 0
            spacing: Style.space(2)

            Text {
              text: String(parent.row.label || parent.row.key || "")
              textFormat: Text.PlainText
              color: Qt.darker(root.foreground, 1.4)
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            ButtonGroup {
              options: parent.row.options || []
              value: String(root.settingValue(String(parent.row.key || "")))
              foreground: root.foreground
              background: root.background
              accent: Commons.Color.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              // ButtonGroup speaks strings; an integer schema key has to arrive at shell.json as a
              // number or `core.as_int` would be coercing a string on every read
              // (`omarchy bar set` without --json stores "1", and bool("false") is true).
              onChanged: function (v) {
                root.writeSetting(String(parent.row.key || ""),
                                  parent.row.type === "integer" ? parseInt(v, 10) : String(v))
              }
            }

            Text {
              width: parent.width
              visible: !!parent.row.description
              text: String(parent.row.description || "")
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }

        Component {
          id: dropdownControl
          Column {
            id: menuControl
            property var row: ({})
            width: parent ? parent.width : 0
            spacing: Style.space(2)
            onRowChanged: root.noteMenu(menuControl.row)

            Connections {
              target: root
              function onSettingsOpenChanged() { if (root.settingsOpen) root.noteMenu(menuControl.row) }
            }

            Dropdown {
              width: Math.min(Style.space(260), parent.width)
              label: String(parent.row.label || parent.row.key || "")
              options: root.optionChoices(parent.row)
              value: String(root.settingValue(String(parent.row.key || "")))
              foreground: root.foreground
              accent: Commons.Color.accent
              fontFamily: root.fontFamily
              onChanged: function (v) {
                root.writeSetting(String(parent.row.key || ""),
                                  parent.row.type === "integer" ? parseInt(v, 10) : String(v))
              }
            }

            Text {
              width: parent.width
              visible: !!parent.row.description
              text: String(parent.row.description || "")
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }

        Component {
          id: numberControl
          Column {
            id: numberCol
            property var row: ({})
            width: parent ? parent.width : 0
            spacing: Style.space(2)

            // One key's control unit is not its stored unit: the subtitle cache is shown in MiB,
            // because 16777216 is not a number anybody wants to type. Its schema min/max are in
            // bytes, so they are converted too rather than duplicated. `null` for every other key.
            readonly property var unit: root.byteUnitOf(String(numberCol.row.key || ""))
            readonly property real factor: numberCol.unit ? Number(numberCol.unit.factor) : 1

            NumberField {
              label: String(numberCol.row.label || numberCol.row.key || "")
                     + (numberCol.unit ? " — " + String(numberCol.unit.label) : "")
              from: numberCol.row.min === undefined ? 0
                    : (numberCol.unit ? Model.bytesToUnit(numberCol.row.min, numberCol.factor)
                                      : Number(numberCol.row.min))
              to: numberCol.row.max === undefined ? 1000
                  : (numberCol.unit ? Model.bytesToUnit(numberCol.row.max, numberCol.factor)
                                    : Number(numberCol.row.max))
              stepSize: numberCol.row.step === undefined ? 1 : Number(numberCol.row.step)
              value: {
                var v = Number(root.settingValue(String(numberCol.row.key || ""))) || 0
                return numberCol.unit ? Model.bytesToUnit(v, numberCol.factor) : v
              }
              foreground: root.foreground
              accent: Commons.Color.accent
              fontFamily: root.fontFamily
              onModified: function (v) {
                root.writeSetting(String(numberCol.row.key || ""),
                                  numberCol.unit ? Model.unitToBytes(v, numberCol.factor) : Number(v))
              }
            }

            Text {
              width: parent.width
              visible: !!numberCol.row.description
              text: String(numberCol.row.description || "")
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }

        Component {
          id: textControl
          Column {
            property var row: ({})
            width: parent ? parent.width : 0
            spacing: Style.space(2)

            Text {
              text: String(parent.row.label || parent.row.key || "")
              textFormat: Text.PlainText
              color: Qt.darker(root.foreground, 1.4)
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            TextField {
              id: valueField
              width: Math.min(Style.space(320), parent.width)
              text: String(root.settingValue(String(parent.row.key || "")) || "")
              // Empty is a setting, not a gap: the font follows the subtitle language.
              placeholderText: parent.row.key === "subtitleFont" ? "Automatic: follows the subtitle language" : ""
              foreground: root.foreground
              accent: Commons.Color.accent
              // Written on Enter or on focus loss, not on every keystroke: one shell.json write per
              // typed character would rewrite the whole bar config thirty times for one font name.
              // Trimmed, because a stray space is what a font-family lookup will not forgive and a
              // Tab through the field is enough to produce one.
              onAccepted: root.writeSetting(String(parent.row.key || ""), String(text).trim())
              onActiveFocusChanged: if (!activeFocus)
                                      root.writeSetting(String(parent.row.key || ""), String(text).trim())
            }

            Text {
              width: parent.width
              visible: !!parent.row.description
              text: String(parent.row.description || "")
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }

        // The window's own status line (`Model.settingsStatus`): a play starting, a settings write that failed, the
        // play's error, a refused play, the restart and standing notices, else the keys. The overlay that also says
        // them is hidden while this window is up.
        Text {
          id: settingsStatus
          anchors.bottom: parent.bottom
          anchors.left: parent.left
          anchors.right: parent.right
          readonly property var line: Model.settingsStatus({
            loaded: !!root.service,
            busy: root.service ? root.service.busy : "",
            playRefusal: root.service ? root.service.playRefusal : "",
            settingsError: root.service ? root.service.settingsError : "",
            lastError: root.service ? root.service.lastError : "",
            errorHeadline: root.service && root.service.lastError ? root.svc.errorHeadline() : "",
            errorDetail: root.service && root.service.lastError ? root.svc.errorDetail() : "",
            restartNotice: root.bridge.notice,
            notice: root.service ? root.service.notice : "",
            back: !root.settingsAlone,
          })
          textFormat: Text.PlainText
          wrapMode: Text.Wrap
          maximumLineCount: 3
          elide: Text.ElideRight
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          color: settingsStatus.line.alert ? Commons.Color.accent : root.dim
          text: settingsStatus.line.text
        }
      }
    }
  }
}
