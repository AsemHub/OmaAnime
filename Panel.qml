import QtQuick
import QtQuick.Controls
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "ServiceLogic.js" as Logic
import "BrowseModel.js" as Model

// Bar face and popout in one entry point (the shape omarchy.audio and omarchy.network use).
// The popout and the bar icon's interactions. All state comes from Service.qml via bar.shell.serviceFor().
Panel {
  id: root
  moduleName: "io.github.asemhub.omaanime"
  // The shell registers the summon/hide/toggle handler on this plugin id itself, and Service.qml
  // owns the "omaanime" IPC target, so this panel manages no IPC of its own.
  manageIpc: false

  readonly property var anime: bar && bar.shell ? bar.shell.serviceFor(moduleName) : null

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.5)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // The Service's effective settings (defaults < config.json < shell.json, typed by `status`), the ones
  // the sheet shows; the host's `setting()` reads only the raw shell.json entry, so a key set in config.json was off
  // here. That raw entry is the fallback until the first `status`. Every monitor's widget reads the one Service, and
  // the bar has one layout, so one entry, for all of them.
  readonly property var effective: anime && anime.settings && typeof anime.settings === "object" ? anime.settings : null
  function barSetting(name, fallback) {
    return effective && effective[name] !== undefined && effective[name] !== null ? effective[name]
                                                                                   : setting(name, fallback)
  }
  readonly property bool showTitle: Logic.asBool(barSetting("showTitleInBar", false), false)
  // `maxTitleWidth` pixels of title, or the whole title for 0 (the manifest's promise; it used to be a fixed 34
  // characters whatever the setting said).
  readonly property var titleCut: Logic.titleElide(barSetting("maxTitleWidth", 180), 180)

  // U+F26C is the television mark (verified present in CaskaydiaMono Nerd Font via fc-list).
  readonly property string glyph: ""

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: bar ? bar.barSize : Style.space(26)

  readonly property var np: anime && anime.nowPlaying ? anime.nowPlaying : null
  readonly property bool loaded: !!anime && anime.running && !anime.idle

  // Every stage a play has passed and the one it is on, not one `busy` line — so
  // the popout says something while mpv's window is off screen, for the whole slow stretch resolve
  // spends searching, ranking and probing providers, not just the "load" tail of it. Degrades to
  // `[]` on anything it cannot read (no play, not running, the writer gone quiet), which
  // is exactly when the status line below takes over instead.
  readonly property var progressRows: anime && anime.playEntry ? Logic.progressRows(anime.playEntry, Date.now() / 1000) : []

  // A play waiting for TorBox, or holding after its wait, offers the viewer's choices: the same buttons and keys
  // as Browse and the player (`Model.playChoice`, the entry's own commands), run by the Service (`runChoice`). A
  // Service older than this file has no `runChoice`: none are shown, and the player and Browse still have them.
  property real choiceClock: Date.now() / 1000
  readonly property var playChoice: anime && typeof anime.runChoice === "function"
                                    ? Model.playChoice(anime.playEntry, choiceClock) : null

  Timer {
    interval: 1000
    repeat: true
    running: root.opened && !!root.anime && !!root.anime.playEntry && !!root.anime.playEntry.choice
    onTriggered: root.choiceClock = Date.now() / 1000
  }

  function runPlayChoice(button) {
    if (!root.anime || !button || !button.runs) return
    var trace = root.anime.runChoice(button)
    root.anime.logEvent(trace || root.anime.newTrace("popout-choice"), "ui.choice.popout",
                        { id: String(button.id || ""), ran: trace !== "" })
  }

  function fullTitle() {
    if (!showTitle || !np || !loaded) return ""
    return String(np.title || "") + " · Ep " + np.episode
  }

  function shortTitle() { return titleMetrics.elidedText }

  // Measured in the button's own font (WidgetButton's `fontFamily` and `fontSize` defaults), so the cut lands where
  // the label would pass `maxTitleWidth`. The button's label has no width or elide of its own to set.
  TextMetrics {
    id: titleMetrics
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    text: root.fullTitle()
    elide: root.titleCut.elide ? Text.ElideRight : Text.ElideNone
    elideWidth: root.titleCut.width
  }

  function entryTitle(entry) {
    if (!entry || !entry.media) return ""
    var t = entry.media.title || {}
    return String(t.english || t.romaji || "")
  }

  function entryLine(entry) {
    if (!entry || !entry.media) return ""
    var n = entry.next || {}
    var total = entry.media.episodes ? " of " + entry.media.episodes : ""
    // AniList lists it as not yet released, with no schedule: nothing to play, and the date when AniList has one.
    var notOut = Model.notOutText(entry.media)
    if (notOut !== "") return notOut
    if (n.episode && n.aired) return "Episode " + n.episode + total
    if (n.episode && !n.aired) return "Ep " + (n.rewatch || entry.progress) + " watched · next airs " + airsIn(n.airsAt)
    if (n.rewatch) return "All " + n.rewatch + " watched · rewatch"
    return "Episode " + ((entry.progress || 0) + 1) + total
  }

  function airsIn(ts) {
    var s = Number(ts) - Date.now() / 1000
    if (!(s > 0)) return "soon"
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600)
    return d > 0 ? "in " + d + "d " + h + "h" : "in " + h + "h"
  }

  function topEntries() {
    if (!anime || !Logic.isList(anime.watching)) return []
    return anime.watching.slice(0, 5)
  }

  // Continue Watching's line over the saved list AniList's outage answered with, or "".
  function savedLineText(nowMs) {
    if (!anime) return ""
    return Logic.listStaleText({ stale: anime.watchingSaved || null, what: "your list", nowMs: nowMs })
  }

  // No image in this process is ever fetched over https (Qt's TLS init once crashed the shell).
  // `coverPath` is a file the CLI
  // downloaded (`omarchy-anime covers warm`, and `play` warms its own); anything that is not an
  // absolute path draws nothing and the placeholder rectangle stays.
  function coverUrl(p) {
    return p && String(p).charAt(0) === "/" ? Util.fileUrl(String(p)) : ""
  }

  function nowPlayingCover() {
    var cp = root.np && root.np.coverPath ? root.np.coverPath : null
    return cp ? root.coverUrl(cp.large || cp.medium) : ""
  }

  // ------------------------------------------------------------- the bar face
  // Ui/WidgetButton with the glyph in `text`: the bar owns pointer input for drag-to-reorder, so a
  // MouseArea of our own never sees the click, and an empty `text` collapses the widget out of the bar.
  WidgetButton {
    id: button
    bar: root.bar
    anchors.centerIn: parent
    text: {
      var t = root.shortTitle()
      return t === "" ? root.glyph : root.glyph + "  " + t
    }
    tooltipText: root.loaded && root.np
      ? String(root.np.title) + " · Ep " + root.np.episode + (root.anime.paused ? " (paused)" : "")
      : "Anime — click for Continue Watching"
    dimmed: !root.anime || !root.anime.running || root.anime.idle
    active: !!root.anime && root.anime.playing
    activeColor: Commons.Color.accent

    onPressed: function (btn) {
      if (btn === Qt.MiddleButton && root.anime) root.anime.toggleWindow()
      else if (btn === Qt.RightButton && root.anime) root.anime.playPause()
      else root.toggle()
    }
  }

  // -------------------------------------------------------------- the popout
  KeyboardPanel {
    id: panel
    bar: root.bar
    anchorItem: button
    owner: root
    open: root.opened
    popoutSwitching: root.popoutSwitching
    popoutSwitchClosing: root.popoutSwitchClosing
    contentWidth: Style.space(340)
    // Measured on this 1080p screen: the binding limit is `availableCardHeight` (the screen minus
    // the bar and the margins), not this cap — with something playing and five Continue Watching
    // rows the content is taller than the space there is, and the last line was cut in half. The
    // cap is raised for headroom on taller displays, and the hint below is one line instead of two.
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(680))
    padding: Style.space(14)
    focusTarget: keyCatcher

    // Escape closes and Tab switches popouts like every first-party panel. KeyboardPanel itself
    // handles neither: the panel content has to host a PanelKeyCatcher (see panels/audio).
    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function (direction) { root.switchPanel(direction) }
      // The waiting play's keys, as on the player and in Browse (`w`, `n`, `d`).
      onTextKey: function (text) {
        var chosen = Model.choiceForKey(root.playChoice, text)
        if (chosen) root.runPlayChoice(chosen)
      }

    // The popout was a plain Column capped by `fittedContentHeight`, so a long Continue Watching
    // list simply ran off the bottom with no way to reach it — no wheel, no bar, nothing. The
    // first-party wrapper (`plugins/panels/audio/Panel.qml:688-698`), `Binding` included: the
    // content item only becomes interactive when there is actually something to scroll, so a
    // short popout does not swallow the wheel.
    ScrollView {
      id: scrollArea
      anchors.fill: parent
      clip: true
      ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
      ScrollBar.vertical.policy: column.implicitHeight > height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff

      Binding {
        target: scrollArea.contentItem
        property: "interactive"
        value: column.implicitHeight > scrollArea.height
      }

    Column {
      id: column
      width: scrollArea.availableWidth
      spacing: Style.space(12)

      // --- now playing ------------------------------------------------------
      Row {
        width: parent.width
        spacing: Style.space(12)
        visible: root.loaded && !!root.np

        Rectangle {
          width: Style.space(64)
          height: Style.space(90)
          radius: Style.space(6)
          color: Qt.darker(root.foreground, 6)
          border.width: 1
          border.color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)
          clip: true

          Image {
            anchors.fill: parent
            source: root.nowPlayingCover()
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            smooth: true
            visible: status === Image.Ready
          }
        }

        Column {
          width: parent.width - Style.space(64) - Style.space(12)
          spacing: Style.space(4)

          Text {
            width: parent.width
            text: root.np ? String(root.np.title || "") : ""
            textFormat: Text.PlainText
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            font.bold: true
            wrapMode: Text.Wrap
            maximumLineCount: 2
            elide: Text.ElideRight
          }

          Text {
            width: parent.width
            text: {
              if (!root.np) return ""
              var s = "Episode " + root.np.episode
              if (root.np.episodeTitle) s += " · " + root.np.episodeTitle
              return s
            }
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }

          // Subtitle badge: the language actually selected, where it came from, and what was done
          // to its timing. The sentence is composed by the CLI (`subs.badge`) and
          // arrives as `subBadge`, so the wording lives next to the data that decides it and the
          // QML stays a renderer. The branch below is the fallback for a now-playing.json written
          // before the CLI composed it, which has no `subBadge`.
          Text {
            width: parent.width
            text: {
              if (!root.np) return ""
              if (root.np.subBadge) return String(root.np.subBadge)
              if (root.np.subFallback)
                return "No " + (root.np.subtitleLanguageName || langName(root.np.subtitleLanguage)) + " subtitles · playing with " + (root.np.subLangName || (root.np.subLang ? String(root.np.subLang).toUpperCase() : "none"))
              return "Subtitles: " + (root.np.subLangName || (root.np.subLang ? String(root.np.subLang).toUpperCase() : "none")) + " · " + String(root.np.provider || "")
            }
            // The CLI names the languages (`subtitleLanguageName`, `subLangName`); a now-playing.json written
            // before has only the codes.
            function langName(code) { return code === "ara" ? "Arabic" : String(code || "").toUpperCase() }
            textFormat: Text.PlainText
            // Accent means "not what you asked for": no preferred language at all, or a signs-only
            // track. A side-load that worked is information, not a warning.
            color: root.np && (root.np.subFallback || root.np.subForced === true) ? Commons.Color.accent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.Wrap
          }

          // Subtitle timing nudge. The estimator gets the offset right when both releases
          // share a downloadable anchor track; when they do not, the track loads unshifted and the
          // badge says "timing unverified". mpv's own escape hatch is z/Z at 0.1 s per press, only
          // while the mpv window holds keyboard focus — 388 presses for the 38.74 s error that
          // produced this control. Shown whenever a subtitle track is selected, or whenever a delay
          // is already applied, so an embedded-but-mistimed track is covered too.
          Row {
            spacing: Style.space(6)
            visible: !!root.np && root.loaded && (!!root.np.subLang || (root.anime && root.anime.subDelay !== 0))

            Button {
              iconText: ""
              tooltipText: "Subtitles 1 s earlier"
              foreground: root.foreground
              accent: Commons.Color.accent
              fontFamily: root.fontFamily
              onClicked: if (root.anime) root.anime.nudgeSubDelay(-1)
            }

            Text {
              anchors.verticalCenter: parent.verticalCenter
              // Signed and always three characters wide at rest, so the row does not jitter as the
              // number changes under a held click.
              text: {
                var d = root.anime ? Number(root.anime.subDelay) : 0
                return (d > 0 ? "+" : "") + d.toFixed(1) + " s"
              }
              textFormat: Text.PlainText
              color: root.anime && root.anime.subDelay !== 0 ? Commons.Color.accent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Button {
              iconText: ""
              tooltipText: "Subtitles 1 s later"
              foreground: root.foreground
              accent: Commons.Color.accent
              fontFamily: root.fontFamily
              onClicked: if (root.anime) root.anime.nudgeSubDelay(1)
            }

            Button {
              iconText: ""
              tooltipText: "Back to the timing the plugin chose"
              visible: !!root.anime && root.anime.subDelay !== 0
              foreground: root.foreground
              accent: Commons.Color.accent
              fontFamily: root.fontFamily
              onClicked: if (root.anime) root.anime.resetSubDelay()
            }
          }

          // The donor release, or the external file, named in full so a bad translation is
          // attributable rather than mysterious. External text: PlainText and elided to the popout width.
          Text {
            width: parent.width
            visible: !!root.np && (root.np.subSource === "sideload" || root.np.subSource === "external") && !!root.np.subDonor
            text: root.np && root.np.subDonor ? "from " + String(root.np.subDonor) : ""
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideMiddle
          }

          Text {
            width: parent.width
            visible: !!root.np && root.np.fromPack === true && !!root.np.file
            text: "From pack: " + (root.np && root.np.file ? String(root.np.file) : "")
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideMiddle
          }

          Text {
            width: parent.width
            visible: !!root.anime && root.anime.cacheStalled
            text: "Buffering…"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }

      // The steps a play has passed and the one it is on — see `root.progressRows`.
      Column {
        width: parent.width
        spacing: Style.space(2)
        visible: root.progressRows.length > 0

        Repeater {
          model: root.progressRows

          Text {
            required property var modelData
            width: parent.width
            text: modelData.text
            textFormat: Text.PlainText
            color: modelData.current ? Commons.Color.accent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.Wrap
          }
        }
      }

      // A play asked for while this one is starting was refused; said here, outside the line below,
      // which hides while the steps above show.
      Text {
        width: parent.width
        visible: !!root.anime && !!root.anime.playRefusal
        text: root.anime && root.anime.playRefusal ? String(root.anime.playRefusal) : ""
        textFormat: Text.PlainText
        color: Commons.Color.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.Wrap
      }

      // Status line when nothing is loaded, no play is in progress, or after an error.
      //
      // `busy` is restored as the LAST fallback, not deleted. It is
      // set synchronously on click (`Service.qml`'s `play()`), while `root.playEntry` — what
      // `root.progressRows` reads — only arrives on the next status poll. Between the click and
      // that poll, with mpv down (no player window yet), `progressRows` is
      // still `[]` and `!root.loaded` is still true, so without `busy` this line would read
      // "Player not running" while a play is in fact being found, and with mpv up it would show
      // nothing at all. `busy` is checked after `lastError`/`notice` — a real error or standing
      // notice is more specific than a generic "still working" line — and before the final
      // "Nothing playing"/"Player not running" default.
      Text {
        width: parent.width
        visible: root.progressRows.length === 0
                && (!root.loaded || (!!root.anime && (root.anime.busy !== "" || root.anime.lastError !== ""
                                                       || root.anime.notice !== "" || !!root.anime.playHeld
                                                       || !!root.anime.playLater)))
        text: {
          if (!root.anime) return "Service not loaded"
          // The cause, then the one thing to do about it, then —
          // when the CLI sent one — the hint behind it. Service.qml sets `lastErrorHeadline` from
          // `Logic.errorSentences` for a play adopted off the poll and for one this Service started
          // itself alike, so it is blank only for "play returned no JSON" — a response that never
          // had a `say` to read — where `errorHeadline()`/`errorAction()` are the old rendering.
          if (root.anime.lastError !== "") {
            var sentenced = root.anime.lastErrorHeadline !== ""
            var headline = sentenced ? root.anime.lastErrorHeadline : root.anime.errorHeadline()
            var action = sentenced ? root.anime.lastErrorAction : root.anime.errorAction()
            var text = headline
            if (action !== "") text += "\n" + action
            if (root.anime.lastErrorHint !== "") text += "\n" + root.anime.lastErrorHint
            return text
          }
          // A standing notice (token expiry, AniList budget), not a failure.
          if (root.anime.notice !== "") return root.anime.notice
          if (root.anime.busy !== "") return root.anime.busy
          // A play holding the viewer's choices after its wait for TorBox: a plain sentence, dimmed, not an
          // error; its choices are the buttons below (an old Service: on the player's loading screen and in Browse).
          if (root.anime.playHeld) return root.anime.playHeld + (root.playChoice ? ""
                                                                 : "\nThe player and Browse have your choices.")
          // "Tell me when it's ready" was recorded: its acknowledgement, for the hour it is current.
          if (root.anime.playLater) return root.anime.playLater
          return root.anime.running ? "Nothing playing" : "Player not running"
        }
        textFormat: Text.PlainText
        color: !!root.anime && (root.anime.lastError !== "" || root.anime.notice !== "")
               ? Commons.Color.accent : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        wrapMode: Text.Wrap
      }

      // The waiting (or held) play's choices: its sentence while it asks, then one button per choice with its key.
      // The live line with TorBox's numbers is the progress row above (running) or the held sentence (held).
      Column {
        width: parent.width
        spacing: Style.space(6)
        visible: !!root.playChoice

        Text {
          width: parent.width
          visible: !!root.playChoice && root.playChoice.headline !== ""
          text: root.playChoice ? root.playChoice.headline : ""
          textFormat: Text.PlainText
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
        }

        Flow {
          width: parent.width
          spacing: Style.space(6)

          Repeater {
            model: root.playChoice ? root.playChoice.buttons : []

            Button {
              required property var modelData
              text: Model.choiceButtonText(modelData)
              enabled: modelData.runs
              bordered: true
              foreground: root.foreground
              accent: Commons.Color.accent
              fontFamily: root.fontFamily
              onClicked: root.runPlayChoice(modelData)
            }
          }
        }
      }

      // A refusal the release picker can answer. The picker lives in the overlay,
      // which takes the keyboard, so the popout closes first (as "Browse…" does).
      Button {
        visible: !!root.anime && root.anime.lastErrorPicker && root.anime.playMediaId > 0
        text: "Choose a release"
        bordered: true
        foreground: root.foreground
        accent: Commons.Color.accent
        fontFamily: root.fontFamily
        onClicked: {
          var m = root.anime.playMediaId, e = root.anime.playEpisode
          root.close()
          root.anime.summonPicker(m, e)
        }
      }

      // --- progress ---------------------------------------------------------
      Item {
        width: parent.width
        height: Style.space(20)
        visible: root.loaded && !!root.anime && root.anime.duration > 0

        Rectangle {
          id: track
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.top: parent.top
          height: Style.space(3)
          radius: height / 2
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.18)

          Rectangle {
            height: parent.height
            radius: parent.radius
            color: Commons.Color.accent
            width: root.anime ? root.anime.percent * parent.width : 0
          }
        }

        Text {
          anchors.left: parent.left
          anchors.top: track.bottom
          anchors.topMargin: Style.space(3)
          text: root.anime ? root.anime.formatTime(root.anime.timePos) : ""
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        Text {
          anchors.right: parent.right
          anchors.top: track.bottom
          anchors.topMargin: Style.space(3)
          text: root.anime ? root.anime.formatTime(root.anime.duration) : ""
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }

      // --- transport --------------------------------------------------------
      Row {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Style.space(14)
        visible: root.loaded

        Button {
          iconText: root.anime && root.anime.paused ? "" : ""
          tooltipText: "Play / pause (right-click the bar icon)"
          bordered: true
          foreground: root.foreground
          accent: Commons.Color.accent
          fontFamily: root.fontFamily
          onClicked: if (root.anime) root.anime.playPause()
        }
        Button {
          iconText: ""
          tooltipText: "Stop and forget this episode"
          foreground: root.foreground
          accent: Commons.Color.accent
          fontFamily: root.fontFamily
          onClicked: if (root.anime) root.anime.stop()
        }
      }

      Toggle {
        width: parent.width
        visible: !!root.anime && root.anime.running
        label: "Player window"
        description: {
          if (!root.anime || !root.anime.running) return "Not running"
          return root.anime.windowVisible ? "On screen" : "Hidden" + (root.anime.playing ? ", still playing" : "")
        }
        checked: !!root.anime && root.anime.windowVisible
        foreground: root.foreground
        accent: Commons.Color.accent
        fontFamily: root.fontFamily
        onClicked: if (root.anime) root.anime.toggleWindow()
      }

      PanelSeparator { width: parent.width; foreground: root.foreground }

      // --- continue watching ------------------------------------------------
      Text {
        text: "CONTINUE WATCHING"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.letterSpacing: 1
      }

      // Loading, signed out, a refused token, offline, failed or empty — one line decided by
      // `Logic.listStateText`, the same one Browse's Continue Watching draws. Worded for the popout,
      // whose Settings button is below it.
      Text {
        width: parent.width
        visible: root.topEntries().length === 0
        text: root.anime ? Logic.listStateText({ loaded: root.anime.watchingLoaded, failure: root.anime.watchingFailure,
                                                 what: "your list", emptyText: Logic.WATCHING_EMPTY,
                                                 where: "popout" }).text
                         : "Loading your list…"
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }

      // The rows are the saved list AniList's outage answered with: one dim line says why and from when,
      // above the rows it is about. The first fresh answer clears `watchingSaved`, and the line with it.
      Text {
        width: parent.width
        visible: text !== "" && root.topEntries().length > 0
        text: root.savedLineText(Date.now())
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

      Repeater {
        model: root.topEntries()

        // The row body was not a click target: only the trailing ▶ was, a ~24 px square in a
        // 340 px row. Same action, whole row, plus the hover feedback that says so.
        //
        // The colour is read from `containsMouse` here and not in Browse.qml, deliberately: the
        // popout has no keyboard cursor of its own (no selection, no j/k), so there is no second
        // highlight for this one to fight with — which is the thing `Ui/CursorSurface`'s contract
        // exists to prevent.
        Item {
          id: cwRow
          required property var modelData
          width: column.width
          implicitHeight: cwContent.implicitHeight
          height: implicitHeight

          Rectangle {
            anchors.fill: parent
            anchors.leftMargin: -Style.space(6)
            anchors.rightMargin: -Style.space(6)
            anchors.topMargin: -Style.space(3)
            anchors.bottomMargin: -Style.space(3)
            radius: Style.cornerRadius
            color: cwMouse.containsMouse ? Style.hoverFillFor(root.foreground, Commons.Color.accent) : "transparent"
          }

          MouseArea {
            id: cwMouse
            anchors.fill: parent
            hoverEnabled: true
            // No hand over an entry that is not out: the click plays nothing (`playNext`), and the line says why.
            cursorShape: Model.notOut(cwRow.modelData ? cwRow.modelData.media : null) ? Qt.ArrowCursor
                                                                                       : Qt.PointingHandCursor
            onClicked: if (root.anime) root.anime.playNext(cwRow.modelData)
          }

        Row {
          id: cwContent
          width: parent.width
          spacing: Style.space(10)

          Column {
            width: parent.width - playButton.width - Style.space(10)
            spacing: Style.space(2)
            anchors.verticalCenter: parent.verticalCenter

            Text {
              width: parent.width
              text: root.entryTitle(cwRow.modelData)
              textFormat: Text.PlainText
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              elide: Text.ElideRight
            }
            Text {
              width: parent.width
              text: root.entryLine(cwRow.modelData)
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }

          Button {
            id: playButton
            anchors.verticalCenter: parent.verticalCenter
            // No play for an entry that is not out: the line says so.
            visible: !Model.notOut(cwRow.modelData ? cwRow.modelData.media : null)
            iconText: cwRow.modelData && cwRow.modelData.next && !cwRow.modelData.next.aired && cwRow.modelData.next.rewatch ? "" : ""
            tooltipText: cwRow.modelData && cwRow.modelData.next && !cwRow.modelData.next.aired ? "Rewatch the last episode" : "Play the next episode"
            foreground: root.foreground
            accent: Commons.Color.accent
            fontFamily: root.fontFamily
            onClicked: if (root.anime) root.anime.playNext(cwRow.modelData)
          }
        }
        }
      }

      // --- browse -----------------------------------------------------------
      // The overlay is this same plugin's `overlay` entry point, so summoning our own id
      // reaches Browse.qml through the shell's panel loader (shell.qml isBarWidgetPanelPlugin
      // is false once a bar widget also declares overlay). The popout closes first: the
      // overlay takes exclusive keyboard focus and two open surfaces would fight for it.
      Row {
        width: parent.width
        spacing: Style.space(8)

        Button {
          width: (parent.width - Style.space(8)) * 0.62
          text: "Browse…"
          bordered: true
          foreground: root.foreground
          accent: Commons.Color.accent
          fontFamily: root.fontFamily
          onClicked: {
            root.close()
            if (root.bar && root.bar.shell) root.bar.shell.summon(root.moduleName, "{}")
          }
        }

        // Omarchy 4.0.3 has no per-widget settings route — no gear, no right-click menu, no
        // omarchy-menu entry. This button is the discoverable way in; the overlay's
        // own `,` and `omarchy-shell omaanime browse settings` are the other two.
        Button {
          width: parent.width - Style.space(8) - (parent.width - Style.space(8)) * 0.62
          text: "Settings"
          iconText: "\uf013"
          bordered: true
          foreground: root.foreground
          accent: Commons.Color.accent
          fontFamily: root.fontFamily
          onClicked: {
            root.close()
            if (root.bar && root.bar.shell)
              root.bar.shell.summon(root.moduleName, JSON.stringify({ view: "settings" }))
          }
        }
      }

      Text {
        width: parent.width
        text: "Middle-click: show/hide · Right-click: pause"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.Wrap
      }
    }
    }
    }
  }
}
