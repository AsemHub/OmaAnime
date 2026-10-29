// An ordered list control, in two modes, for the settings sheet's Advanced disclosure.
//
//   options non-empty  -> the rows are fixed and can only be toggled and reordered (`providers`)
//   options empty      -> the rows are editable free text with add/remove (`indexers`, the group lists)
//
// **Not `qs.Ui.MultiSelect`.** It exists (`/usr/share/omarchy/shell/Ui/MultiSelect.qml`, registered
// in qmldir, instantiated by zero files) and its API is close — `values: []` plus
// `changed(var values)` — but its popup is a checkbox list: it models set *membership* and cannot
// express order, which is the entire semantics of these keys. Provider order decides which debrid
// service is tried first; `indexers` order decides which add-on is asked first.
//
// The pure halves live in BrowseModel.js (`reorder`, `toggleIn`, `asList`, `listRowShape`) so
// node's test runner can reach them; this file is layout and wiring.
import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "BrowseModel.js" as Model

Column {
  id: root

  // The current value, in order. Accepts the comma-joined string the settings actually store and
  // a real array from a hand-edited config.json alike.
  property var values: []
  // Non-empty makes the rows fixed: these are the only entries, toggled on and off.
  property var options: []
  // Free-text mode only: a row is refused unless this returns true.
  property var validator: null
  property string addLabel: "Add"
  property string placeholder: ""

  property color foreground: Commons.Color.foreground
  property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.55)
  property string fontFamily: Style.font.family

  // The whole list, every time — the caller writes one key, so a partial update would be a
  // read-modify-write with nothing to read it back from.
  signal changed(var values)

  readonly property var current: Model.asList(values)
  readonly property bool fixed: !!(options && options.length > 0)

  // Fixed mode shows the enabled entries in their stored order first, then the ones that are off,
  // so "what is tried first" reads top to bottom and turning something on appends it at the end.
  readonly property var rows: {
    if (!root.fixed) return root.current
    var out = []
    var i
    for (i = 0; i < root.current.length; i++)
      if (options.indexOf(root.current[i]) !== -1 && out.indexOf(root.current[i]) === -1)
        out.push(root.current[i])
    for (i = 0; i < options.length; i++)
      if (out.indexOf(String(options[i])) === -1) out.push(String(options[i]))
    return out
  }

  function isOn(name) { return root.current.indexOf(String(name)) !== -1 }

  function emitValues(next) { root.changed(next) }

  function move(index, direction) {
    // In fixed mode the visible order is enabled-then-disabled, so a move only means something
    // between two enabled rows; reorder() itself refuses an out-of-range move.
    root.emitValues(Model.reorder(root.fixed ? root.current : root.rows, index, direction))
  }

  function toggle(name) { root.emitValues(Model.toggleIn(root.current, name, root.options)) }

  function setAt(index, text) {
    var next = root.current.slice()
    if (index < 0 || index >= next.length) return
    next[index] = String(text)
    root.emitValues(next)
  }

  function removeAt(index) {
    var next = root.current.slice()
    if (index < 0 || index >= next.length) return
    next.splice(index, 1)
    root.emitValues(next)
  }

  function accepts(text) {
    if (typeof root.validator !== "function") return String(text).length > 0
    return root.validator(text) === true
  }

  spacing: Style.space(4)

  Repeater {
    model: root.rows

    Row {
      id: entry
      required property int index
      required property var modelData
      width: root.width
      spacing: Style.space(6)

      readonly property bool on: root.fixed ? root.isOn(entry.modelData) : true
      // Only an enabled row has a place in the order, and only between two of them.
      readonly property int orderIndex: root.fixed ? root.current.indexOf(String(entry.modelData)) : entry.index
      readonly property int orderCount: root.fixed ? root.current.length : root.rows.length

      Button {
        anchors.verticalCenter: parent.verticalCenter
        visible: root.fixed
        width: visible ? Style.space(28) : 0
        text: entry.on ? "✓" : "·"
        tooltipText: entry.on ? "Enabled — click to turn off" : "Disabled — click to turn on"
        selected: entry.on
        foreground: root.foreground
        accent: Commons.Color.accent
        fontFamily: root.fontFamily
        fontSize: Style.font.bodySmall
        onClicked: root.toggle(entry.modelData)
      }

      Text {
        anchors.verticalCenter: parent.verticalCenter
        visible: root.fixed
        width: visible ? Style.space(96) : 0
        text: String(entry.modelData)
        textFormat: Text.PlainText
        color: entry.on ? root.foreground : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        elide: Text.ElideRight
      }

      TextField {
        id: field
        anchors.verticalCenter: parent.verticalCenter
        visible: !root.fixed
        width: visible ? root.width - Style.space(96) : 0
        text: root.fixed ? "" : String(entry.modelData)
        placeholderText: root.placeholder
        foreground: root.foreground
        accent: Commons.Color.accent
        font.family: root.fontFamily
        // Committed on Enter and on losing focus, never per keystroke: every keystroke would be a
        // shell.json write, and half a hostname is not a hostname.
        onAccepted: if (root.accepts(text)) root.setAt(entry.index, text); else text = String(entry.modelData)
        onActiveFocusChanged: if (!activeFocus) {
          if (text !== String(entry.modelData) && root.accepts(text)) root.setAt(entry.index, text)
          else if (!root.accepts(text)) text = String(entry.modelData)
        }
      }

      Button {
        anchors.verticalCenter: parent.verticalCenter
        enabled: entry.orderIndex > 0
        opacity: enabled ? 1 : 0.35
        text: "▲"
        tooltipText: "Move up"
        foreground: root.foreground
        accent: Commons.Color.accent
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        onClicked: root.move(entry.orderIndex, -1)
      }

      Button {
        anchors.verticalCenter: parent.verticalCenter
        enabled: entry.orderIndex >= 0 && entry.orderIndex < entry.orderCount - 1
        opacity: enabled ? 1 : 0.35
        text: "▼"
        tooltipText: "Move down"
        foreground: root.foreground
        accent: Commons.Color.accent
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        onClicked: root.move(entry.orderIndex, 1)
      }

      Button {
        anchors.verticalCenter: parent.verticalCenter
        visible: !root.fixed
        width: visible ? Style.space(28) : 0
        text: "×"
        tooltipText: "Remove"
        foreground: root.foreground
        accent: Commons.Color.accent
        fontFamily: root.fontFamily
        fontSize: Style.font.bodySmall
        onClicked: root.removeAt(entry.index)
      }
    }
  }

  Row {
    width: root.width
    spacing: Style.space(6)
    visible: !root.fixed

    TextField {
      id: adder
      width: root.width - Style.space(96)
      placeholderText: root.placeholder
      foreground: root.foreground
      accent: Commons.Color.accent
      font.family: root.fontFamily
      onAccepted: {
        if (!root.accepts(text)) return
        var next = root.current.slice()
        next.push(String(text))
        root.emitValues(next)
        text = ""
      }
    }

    Button {
      anchors.verticalCenter: parent.verticalCenter
      text: root.addLabel
      bordered: true
      enabled: root.accepts(adder.text)
      opacity: enabled ? 1 : 0.35
      foreground: root.foreground
      accent: Commons.Color.accent
      fontFamily: root.fontFamily
      fontSize: Style.font.caption
      onClicked: {
        if (!root.accepts(adder.text)) return
        var next = root.current.slice()
        next.push(String(adder.text))
        root.emitValues(next)
        adder.text = ""
      }
    }
  }
}
