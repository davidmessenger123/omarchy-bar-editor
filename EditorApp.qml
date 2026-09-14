import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Wayland
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Editor.js" as Editor

// Omarchy Bar Editor — standalone application.
//
// A full-screen PanelWindow that inherits Omarchy theming via the app config
// dir's symlinked Commons / Ui modules. Changes are written atomically to
// shell.json and shell.toml through shell_io.py; the shell hot-reloads after
// a successful save while this process survives independently.
PanelWindow {
  id: root

  visible: true
  anchors { top: true; bottom: true; left: true; right: true }
  color: "transparent"
  exclusionMode: ExclusionMode.Ignore
  WlrLayershell.namespace: "omarchy-bar-editor"
  WlrLayershell.layer: WlrLayer.Overlay
  WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive

  readonly property string pluginDir: String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")

  property var cfg: ({})
  property var catalog: ({})
  property var widgetStates: ({})
  property var toml: ({"values": {}, "exists": false})
  property var profiles: []
  property string statusText: ""
  property bool dirty: false
  property string globalFilter: ""

  property var undoStack: []
  property var redoStack: []
  readonly property int undoLimit: 100

  property var leftList: []
  property var centerList: []
  property var rightList: []

  property bool transparentValue: false
  property bool scaleFontValue: true

  readonly property string fontFamily: Style.font.family

  function ensureCfg() {
    return Editor.ensureCfg(root.cfg)
  }

  function secList(sec) {
    if (sec === "left") return root.leftList
    if (sec === "center") return root.centerList
    if (sec === "right") return root.rightList
    return []
  }

  function setSecList(sec, arr) {
    if (sec === "left") root.leftList = arr
    else if (sec === "center") root.centerList = arr
    else if (sec === "right") root.rightList = arr
  }

  function refreshLayout() {
    Editor.SECTIONS.forEach(function(sec) {
      var raw = root.cfg.bar && root.cfg.bar.layout ? root.cfg.bar.layout[sec] : []
      var fil = root.globalFilter.toLowerCase()
      var arr = (raw || []).map(function(item, idx) {
        var wid = (item && item.id) || item || ""
        if (fil && wid.toLowerCase().indexOf(fil) === -1) return null
        return { id: wid, realIndex: idx, section: sec }
      }).filter(function(x) { return x !== null })
      root.setSecList(sec, arr)
    })
  }

  function addOptions() {
    var keys = Object.keys(root.catalog || {})
    var states = root.widgetStates || {}
    return keys.filter(function(k) {
      return states[k] !== false
    }).map(function(k) {
      return { value: k, label: Editor.displayName(root.catalog, k) }
    }).sort(function(a, b) { return a.label.localeCompare(b.label) })
  }

  function pushHistory() {
    root.undoStack.push({
      cfg: JSON.parse(JSON.stringify(root.cfg)),
      toml: JSON.parse(JSON.stringify(root.toml))
    })
    root.redoStack = []
    if (root.undoStack.length > root.undoLimit) root.undoStack.shift()
  }

  function undo() {
    if (!root.undoStack.length) return
    root.redoStack.push({
      cfg: JSON.parse(JSON.stringify(root.cfg)),
      toml: JSON.parse(JSON.stringify(root.toml))
    })
    var prev = root.undoStack.pop()
    root.cfg = prev.cfg
    root.toml = prev.toml
    root.syncFromCfgCore()
    root.refreshLayout()
  }

  function redo() {
    if (!root.redoStack.length) return
    root.undoStack.push({
      cfg: JSON.parse(JSON.stringify(root.cfg)),
      toml: JSON.parse(JSON.stringify(root.toml))
    })
    var next = root.redoStack.pop()
    root.cfg = next.cfg
    root.toml = next.toml
    root.syncFromCfgCore()
    root.refreshLayout()
  }

  function markDirty() {
    root.dirty = true
    root.statusText = "Unsaved changes"
  }

  function markClean() {
    root.dirty = false
  }

  function loadConfig() {
    readProcess.running = true
    catalogProcess.running = true
    statesProcess.running = true
  }

  function applyFromCfg() {
    var cfg = root.cfg || {}
    var bar = cfg.bar || {}
    var layout = bar.layout || {}
    positionDropdown.value = bar.position || "top"
    root.transparentValue = bar.transparent === true
    anchorField.text = bar.centerAnchor || ""
    root.scaleFontValue = bar.scaleWithFont !== false
    fontFamilyField.text = bar.fontFamily || ""
    screensaverField.value = (cfg.idle && cfg.idle.screensaver) || 0
    lockField.value = (cfg.idle && cfg.idle.lock) || 0
    bgColorField.value = (root.toml.values && root.toml.values.background) || "#1a1b26"
    textColorField.value = (root.toml.values && root.toml.values.text) || "#c0caf5"
    activeColorField.value = (root.toml.values && root.toml.values.active) || "#f7768e"
    alphaField.value = (root.toml.values && root.toml.values.alpha) || 100
    sizeHField.value = (root.toml.values && root.toml.values.sizeH) || 30
    sizeVField.value = (root.toml.values && root.toml.values.sizeV) || 30
    hostDropdown.value = bar.host || "omarchy.bar"
    root.syncFromCfgCore()
    root.refreshLayout()
    root.markClean()
  }

  function syncFromCfgCore() {
    root.transparentValue = (root.cfg.bar && root.cfg.bar.transparent) || false
    root.scaleFontValue = (root.cfg.bar && root.cfg.bar.scaleWithFont !== false)
  }

  function gatherCfg() {
    var cfg = Editor.ensureCfg(root.cfg)
    cfg.bar = cfg.bar || {}
    cfg.bar.position = positionDropdown.value || "top"
    cfg.bar.transparent = root.transparentValue
    cfg.bar.centerAnchor = anchorField.text || ""
    cfg.bar.scaleWithFont = root.scaleFontValue
    cfg.bar.fontFamily = fontFamilyField.text || ""
    cfg.bar.layout = cfg.bar.layout || {}
    cfg.bar.layout.left = root.leftList.map(function(e) { return e.id ? { id: e.id } : e.id })
    cfg.bar.layout.center = root.centerList.map(function(e) { return e.id ? { id: e.id } : e.id })
    cfg.bar.layout.right = root.rightList.map(function(e) { return e.id ? { id: e.id } : e.id })
    cfg.idle = cfg.idle || {}
    cfg.idle.screensaver = screensaverField.value || 0
    cfg.idle.lock = lockField.value || 0
    return cfg
  }

  function gatherToml() {
    var t = JSON.parse(JSON.stringify(root.toml))
    t.values = t.values || {}
    t.values.background = bgColorField.value
    t.values.text = textColorField.value
    t.values.active = activeColorField.value
    t.values.alpha = alphaField.value
    t.values.sizeH = sizeHField.value
    t.values.sizeV = sizeVField.value
    return t
  }

  function save() {
    root.pushHistory()
    var cfg = root.gatherCfg()
    var toml = root.gatherToml()
    writeProcess.pendingWrite = JSON.stringify({ write: { shell: cfg, toml: toml }})
    writeProcess.running = true
    root.statusText = "Saving…"
  }

  function addWidget(sec, wid) {
    root.pushHistory()
    var list = secList(sec).slice()
    list.push({ id: wid, realIndex: list.length, section: sec })
    setSecList(sec, list)
    root.markDirty()
    root.refreshLayout()
  }

  function removeWidget(sec, idx) {
    root.pushHistory()
    var list = secList(sec).slice()
    list.splice(idx, 1)
    setSecList(sec, list)
    root.markDirty()
    root.refreshLayout()
  }

  function moveWidget(sec, idx, delta) {
    var list = secList(sec).slice()
    var target = idx + delta
    if (target < 0 || target >= list.length) return
    root.pushHistory()
    var tmp = list[idx]
    list[idx] = list[target]
    list[target] = tmp
    setSecList(sec, list)
    root.markDirty()
    root.refreshLayout()
  }

  function moveSection(sec, idx, targetSec) {
    if (!targetSec || sec === targetSec) return
    root.pushHistory()
    var from = secList(sec).slice()
    var to = secList(targetSec).slice()
    var item = from.splice(idx, 1)[0]
    item.section = targetSec
    to.push(item)
    setSecList(sec, from)
    setSecList(targetSec, to)
    root.markDirty()
    root.refreshLayout()
  }

  function neighborSection(sec, delta) {
    var s = Editor.SECTIONS
    var i = s.indexOf(sec) + delta
    return (i >= 0 && i < s.length) ? s[i] : null
  }

  function resetBar() {
    resetConfirm.opened = true
  }

  function confirmReset() {
    resetProcess.running = true
  }

  function toggleBarVisible() {
    toggleBarProcess.running = true
  }

  function closeEditor() {
    quitProcess.running = true
  }

  // --- startup --------------------------------------------------------------

  Component.onCompleted: {
    keysCatch.forceActiveFocus()
    root.loadConfig()
  }

  // --- scrim ----------------------------------------------------------------

  Rectangle {
    anchors.fill: parent
    color: Util.alpha(Color.background, 0.78)

    MouseArea {
      anchors.fill: parent
      onClicked: root.closeEditor()
    }
  }

  // --- keyboard dispatch ----------------------------------------------------

  Item {
    id: keysCatch
    anchors.fill: parent
    focus: true
    Keys.priority: Keys.BeforeItem
    Keys.onPressed: function(event) {
      if (resetConfirm.opened) {
        if (resetConfirm.handleKey(event)) event.accepted = true
        return
      }
      if (event.key === Qt.Key_Escape) {
        root.closeEditor(); event.accepted = true
      } else if (event.modifiers === Qt.ControlModifier && event.key === Qt.Key_S) {
        root.save(); event.accepted = true
      } else if (event.modifiers === Qt.ControlModifier && event.key === Qt.Key_R) {
        root.loadConfig(); event.accepted = true
      } else if (event.modifiers === Qt.ControlModifier && !(event.modifiers & Qt.ShiftModifier) && event.key === Qt.Key_Z) {
        root.undo(); event.accepted = true
      } else if (event.modifiers === (Qt.ControlModifier | Qt.ShiftModifier) && event.key === Qt.Key_Z) {
        root.redo(); event.accepted = true
      }
    }

    Rectangle {
      id: card
      anchors.centerIn: parent
      width: parent.width - 2 * Style.space(80)
      height: parent.height - 2 * Style.space(64)
      radius: Style.cornerRadius
      color: Color.popups.background
      border.width: Math.max(1, Style.space(2))
      border.color: Color.popups.border
      clip: true

      ColumnLayout {
        anchors.fill: parent
        anchors.margins: Style.space(16)
        spacing: Style.space(12)

        // ---- header --------------------------------------------------
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(8)

          Text {
            text: "BAR  EDITOR"
            color: Color.accent
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
            font.letterSpacing: 2
            Layout.fillWidth: true
          }

          Button {
            text: "Undo"
            tooltipText: "Undo last change (Ctrl+Z)"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            horizontalPadding: Style.space(10)
            verticalPadding: Style.space(3)
            onClicked: root.undo()
          }
          Button {
            text: "Redo"
            tooltipText: "Redo (Ctrl+Shift+Z)"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            horizontalPadding: Style.space(10)
            verticalPadding: Style.space(3)
            onClicked: root.redo()
          }
          Button {
            text: "Reload"
            tooltipText: "Reload config from disk (Ctrl+R)"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            horizontalPadding: Style.space(10)
            verticalPadding: Style.space(3)
            onClicked: root.loadConfig()
          }
          Button {
            text: "Reset"
            tooltipText: "Reset bar to Omarchy defaults"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            horizontalPadding: Style.space(10)
            verticalPadding: Style.space(3)
            onClicked: root.resetBar()
          }
          Button {
            text: "Save"
            tooltipText: "Save changes (Ctrl+S)"
            bordered: true
            foreground: Color.accent
            accent: Color.accent
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            horizontalPadding: Style.space(14)
            verticalPadding: Style.space(3)
            onClicked: root.save()
          }
          Button {
            text: "\uf00d"
            tooltipText: "Close (Esc)"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            horizontalPadding: Style.space(10)
            verticalPadding: Style.space(3)
            onClicked: root.closeEditor()
          }
        }

        // ---- body ----------------------------------------------------
        RowLayout {
          Layout.fillWidth: true
          Layout.fillHeight: true
          spacing: Style.space(16)

          // ---- left: settings column -------------------------------
          ColumnLayout {
            Layout.preferredWidth: Style.space(290)
            Layout.fillHeight: true
            spacing: Style.space(10)

            // BAR SETTINGS
            Rectangle {
              Layout.fillWidth: true
              radius: Style.cornerRadius
              color: Util.alpha(Color.foreground, 0.05)
              implicitHeight: barSettingsCol.implicitHeight + 2 * Style.space(12)

              ColumnLayout {
                id: barSettingsCol
                anchors.fill: parent
                anchors.margins: Style.space(12)
                spacing: Style.space(10)

                PanelSectionHeader { text: "BAR SETTINGS" }

                Dropdown {
                  id: positionDropdown
                  Layout.fillWidth: true
                  label: "Position"
                  options: Editor.POSITIONS
                  value: "top"
                  onChanged: root.markDirty()
                }

                Toggle {
                  Layout.fillWidth: true
                  label: "Transparent bar"
                  description: "Transparent bar background"
                  checked: root.transparentValue
                  onClicked: { root.transparentValue = !root.transparentValue; root.markDirty() }
                }

                ColumnLayout {
                  spacing: Style.spacing.labelGap
                  Layout.fillWidth: true
                  Text {
                    textFormat: Text.PlainText
                    text: "Center anchor widget"
                    color: Qt.darker(Color.foreground, 1.4)
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                  }
                  TextField {
                    id: anchorField
                    Layout.fillWidth: true
                    font.pixelSize: Style.font.bodySmall
                    placeholderText: "e.g. omarchy.clock"
                    selectByMouse: true
                    onTextChanged: root.markDirty()
                  }
                }

                Dropdown {
                  id: hostDropdown
                  Layout.fillWidth: true
                  label: "Bar host"
                  options: []
                  value: "omarchy.bar"
                  onChanged: root.markDirty()
                }
              }
            }

            // FONT
            Rectangle {
              Layout.fillWidth: true
              radius: Style.cornerRadius
              color: Util.alpha(Color.foreground, 0.05)
              implicitHeight: fontCol.implicitHeight + 2 * Style.space(12)

              ColumnLayout {
                id: fontCol
                anchors.fill: parent
                anchors.margins: Style.space(12)
                spacing: Style.space(10)

                PanelSectionHeader { text: "FONT" }
                TextField {
                  id: fontFamilyField
                  Layout.fillWidth: true
                  font.pixelSize: Style.font.bodySmall
                  placeholderText: "System default"
                  selectByMouse: true
                  onTextChanged: root.markDirty()
                }
              }
            }

            // IDLE
            Rectangle {
              Layout.fillWidth: true
              radius: Style.cornerRadius
              color: Util.alpha(Color.foreground, 0.05)
              implicitHeight: idleCol.implicitHeight + 2 * Style.space(12)

              ColumnLayout {
                id: idleCol
                anchors.fill: parent
                anchors.margins: Style.space(12)
                spacing: Style.space(10)

                PanelSectionHeader { text: "IDLE / LOCK" }

                NumberField {
                  id: screensaverField
                  label: "Screensaver (s)"
                  from: 0; to: 86400; stepSize: 10
                  onModified: root.markDirty()
                }
                NumberField {
                  id: lockField
                  label: "Lock (s)"
                  from: 0; to: 86400; stepSize: 10
                  onModified: root.markDirty()
                }
              }
            }

            // STYLING
            Rectangle {
              Layout.fillWidth: true
              Layout.fillHeight: true
              radius: Style.cornerRadius
              color: Util.alpha(Color.foreground, 0.05)

              ColumnLayout {
                anchors.fill: parent
                anchors.margins: Style.space(12)
                spacing: Style.space(10)

                PanelSectionHeader { text: "BAR STYLING (shell.toml)" }

                ColorPicker {
                  id: bgColorField
                  Layout.fillWidth: true
                  label: "Background"
                  value: "#1a1b26"
                  onChanged: root.markDirty()
                }
                ColorPicker {
                  id: textColorField
                  Layout.fillWidth: true
                  label: "Text"
                  value: "#c0caf5"
                  onChanged: root.markDirty()
                }
                ColorPicker {
                  id: activeColorField
                  Layout.fillWidth: true
                  label: "Active"
                  value: "#f7768e"
                  onChanged: root.markDirty()
                }
                NumberField {
                  id: alphaField
                  label: "Background alpha (%)"
                  from: 0; to: 100; stepSize: 5
                  onModified: root.markDirty()
                }
                NumberField {
                  id: sizeHField
                  label: "Horizontal size (px)"
                  from: 1; to: 200; stepSize: 1
                  onModified: root.markDirty()
                }
                NumberField {
                  id: sizeVField
                  label: "Vertical size (px)"
                  from: 1; to: 200; stepSize: 1
                  onModified: root.markDirty()
                }
                Toggle {
                  Layout.fillWidth: true
                  label: "Scale with font"
                  checked: root.scaleFontValue
                  onClicked: { root.scaleFontValue = !root.scaleFontValue; root.markDirty() }
                }

                Item { Layout.fillHeight: true }

                Button {
                  Layout.fillWidth: true
                  text: "Toggle bar on/off"
                  tooltipText: "Show/hide the bar"
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  horizontalPadding: Style.space(10)
                  verticalPadding: Style.space(3)
                  onClicked: root.toggleBarVisible()
                }
              }
            }
          }

          // ---- right: layout editor ---------------------------------
          ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: Style.space(10)

            PanelSectionHeader { text: "BAR  LAYOUT" }

            TextField {
              id: globalSearchField
              Layout.fillWidth: true
              font.pixelSize: Style.font.bodySmall
              placeholderText: "Search across all sections (Ctrl+F)…"
              selectByMouse: true
              onTextChanged: { root.globalFilter = text; root.refreshLayout() }
            }

            RowLayout {
              Layout.fillWidth: true
              Layout.fillHeight: true
              spacing: Style.space(10)

              Repeater {
                model: Editor.SECTIONS

                Rectangle {
                  Layout.preferredWidth: 1
                  Layout.fillWidth: true
                  Layout.fillHeight: true
                  radius: Style.cornerRadius
                  color: Util.alpha(Color.foreground, 0.05)
                  clip: true

                  ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: Style.space(10)
                    spacing: Style.space(8)

                    // section header: label + count
                    RowLayout {
                      Layout.fillWidth: true
                      Text {
                        Layout.fillWidth: true
                        text: Editor.SECTION_LABELS[modelData].toUpperCase()
                        color: Color.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: true
                        elide: Text.ElideRight
                      }
                      Text {
                        text: root.secList(modelData).length
                        color: Qt.darker(Color.foreground, 1.4)
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                      }
                    }

                    ListView {
                      id: sectionList
                      Layout.fillWidth: true
                      Layout.fillHeight: true
                      clip: true
                      spacing: Style.space(4)
                      model: root.secList(modelData)

                      delegate: Rectangle {
                        required property var modelData
                        required property int index
                        width: sectionList.width
                        height: rowBody.implicitHeight + Style.space(8)
                        radius: Style.cornerRadius
                        color: Util.alpha(Color.foreground, 0.03)

                        readonly property var entry: modelData || {}
                        readonly property var wid: entry.id || ""
                        readonly property string sec: entry.section || ""
                        readonly property int realIndex: entry.realIndex

                        RowLayout {
                          id: rowBody
                          anchors.left: parent.left
                          anchors.right: parent.right
                          anchors.verticalCenter: parent.verticalCenter
                          anchors.leftMargin: Style.space(8)
                          anchors.rightMargin: Style.space(8)
                          spacing: Style.space(6)

                          Rectangle {
                            Layout.preferredWidth: Style.space(6)
                            Layout.preferredHeight: Style.space(6)
                            radius: Math.ceil(Style.space(3))
                            color: Editor.catColor(root.catalog, wid || "")
                          }

                          ColumnLayout {
                            spacing: 0
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Text {
                              text: Editor.displayName(root.catalog, wid)
                              color: Color.foreground
                              font.family: root.fontFamily
                              font.pixelSize: Style.font.bodySmall
                              font.bold: true
                              elide: Text.ElideRight
                              Layout.fillWidth: true
                            }
                            Text {
                              text: wid
                              color: Qt.darker(Color.foreground, 1.4)
                              font.family: root.fontFamily
                              font.pixelSize: Style.font.caption
                              elide: Text.ElideRight
                              Layout.fillWidth: true
                            }
                          }

                          Button {
                            text: "\uf077"
                            tooltipText: "Move up"
                            fontFamily: root.fontFamily
                            fontSize: Style.font.caption
                            horizontalPadding: Style.space(5)
                            verticalPadding: Style.space(2)
                            onClicked: root.moveWidget(sec, realIndex, -1)
                          }
                          Button {
                            text: "\uf078"
                            tooltipText: "Move down"
                            fontFamily: root.fontFamily
                            fontSize: Style.font.caption
                            horizontalPadding: Style.space(5)
                            verticalPadding: Style.space(2)
                            onClicked: root.moveWidget(sec, realIndex, 1)
                          }
                          Button {
                            text: "\uf053"
                            tooltipText: "To previous section"
                            fontFamily: root.fontFamily
                            fontSize: Style.font.caption
                            horizontalPadding: Style.space(5)
                            verticalPadding: Style.space(2)
                            onClicked: root.moveSection(sec, realIndex, root.neighborSection(sec, -1))
                          }
                          Button {
                            text: "\uf054"
                            tooltipText: "To next section"
                            fontFamily: root.fontFamily
                            fontSize: Style.font.caption
                            horizontalPadding: Style.space(5)
                            verticalPadding: Style.space(2)
                            onClicked: root.moveSection(sec, realIndex, root.neighborSection(sec, 1))
                          }
                          Button {
                            text: "\uf1f8"
                            tooltipText: "Remove"
                            fontFamily: root.fontFamily
                            fontSize: Style.font.caption
                            horizontalPadding: Style.space(5)
                            verticalPadding: Style.space(2)
                            onClicked: root.removeWidget(sec, realIndex)
                          }
                        }
                      }
                    }

                    // add widget
                    EditorSearchableDropdown {
                      id: addDrop
                      Layout.fillWidth: true
                      label: "Add widget"
                      triggerLabel: "+ Add widget"
                      placeholderText: "Search widgets…"
                      emptyText: "No matching widgets"
                      options: root.addOptions()
                      onChanged: {
                        if (!value) return
                        root.addWidget(modelData, value)
                        addDrop.value = ""
                      }
                    }
                  }
                }
              }
            }
          }
        }

        // ---- footer / status -----------------------------------------
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(8)

          Rectangle {
            Layout.preferredWidth: Style.space(8)
            Layout.preferredHeight: Style.space(8)
            radius: Math.ceil(Style.space(4))
            color: root.dirty ? Color.urgent : "#3fb950"
          }

          Text {
            Layout.fillWidth: true
            text: root.statusText
            color: Qt.darker(Color.foreground, 1.4)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }

          Text {
            text: "Ctrl+S save · Ctrl+R reload · Esc close"
            color: Qt.darker(Color.foreground, 1.6)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }

      // reset confirmation (rendered above the card)
      ConfirmDialog {
        id: resetConfirm
        anchors.fill: parent
        opened: false
        message: "Reset the bar to Omarchy defaults?"
        confirmText: "Reset"
        onConfirmed: root.confirmReset()
        onCanceled: resetConfirm.opened = false
      }
    }
  }

  // ---- processes ----------------------------------------------------------

  Process {
    id: readProcess
    running: false
    command: ["python3", root.pluginDir + "shell_io.py", "read"]
    stdout: StdioCollector {
      id: readOutput
      waitForEnd: true
    }
    onExited: function(exitCode) {
      var text = readOutput.text.trim()
      if (exitCode !== 0 || !text) {
        root.statusText = "Could not read config"
        return
      }
      try {
        var parsed = JSON.parse(text)
        root.cfg = Editor.ensureCfg(parsed.cfg || {})
        root.toml = parsed.toml || {values: {}, exists: false}
        root.profiles = parsed.profiles || []
        root.applyFromCfg()
      } catch (e) {
        root.statusText = "Invalid config state"
      }
    }
  }

  Process {
    id: catalogProcess
    running: false
    command: ["omarchy-plugin-catalog"]
    stdout: StdioCollector {
      id: catalogOutput
      waitForEnd: true
    }
    onExited: function(exitCode) {
      var text = catalogOutput.text.trim()
      if (exitCode !== 0 || !text) return
      try {
        var entries = JSON.parse(text)
        var cat = {}
        for (var i = 0; i < entries.length; i++) {
          var e = entries[i]
          if (e && e.id) cat[e.id] = e
        }
        root.catalog = cat
        hostDropdown.options = Editor.hosts(cat).map(function(h) {
          return {value: h, label: h === "omarchy.bar" ? "Omarchy (default)" : h}
        })
        root.refreshLayout()
      } catch (e) {}
    }
  }

  Process {
    id: statesProcess
    running: false
    command: ["omarchy", "plugin", "list", "--json"]
    stdout: StdioCollector {
      id: statesOutput
      waitForEnd: true
    }
    onExited: function(exitCode) {
      var text = statesOutput.text.trim()
      if (exitCode !== 0 || !text) return
      try {
        var entries = JSON.parse(text)
        var st = {}
        for (var i = 0; i < entries.length; i++) {
          var e = entries[i]
          if (e && e.id && e.enabled !== undefined) st[e.id] = e.enabled
        }
        root.widgetStates = st
      } catch (e) {}
    }
  }

  Process {
    id: writeProcess
    running: false
    stdinEnabled: true
    property string pendingWrite: ""
    onStarted: {
      if (writeProcess.pendingWrite !== "") {
        writeProcess.write(writeProcess.pendingWrite)
        writeProcess.pendingWrite = ""
      }
    }
    onExited: function(exitCode) {
      writeProcess.pendingWrite = ""
      if (exitCode === 0) {
        root.markClean()
        root.refreshLayout()
        root.statusText = "Saved — the shell reloads automatically"
      } else {
        root.statusText = "Save failed — check the logs"
      }
    }
  }

  Process {
    id: pluginEnableProcess
    running: false
  }

  Process {
    id: resetProcess
    running: false
    command: ["omarchy", "bar", "defaults"]
    onExited: function(exitCode) {
      resetConfirm.opened = false
      if (exitCode === 0) {
        root.statusText = "Reset to default Omarchy bar"
        root.loadConfig()
      } else {
        root.statusText = "Reset failed"
      }
    }
  }

  Process {
    id: toggleBarProcess
    running: false
    command: ["omarchy", "toggle", "bar"]
    onExited: function(exitCode) {
      if (exitCode === 0) root.statusText = "Bar visibility toggled"
    }
  }

  // Exit the process by sending SIGTERM to ourselves from a child shell.
  // This matches how omarchy-restart-shell terminates the main shell.
  Process {
    id: quitProcess
    running: false
    command: ["sh", "-c", "kill -TERM " + String(Quickshell.processId)]
  }
}
